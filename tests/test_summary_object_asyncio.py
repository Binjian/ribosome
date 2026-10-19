"""Offline coverage for two-corpus object summarization and its notebook."""
import ast
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from nbformat import read, validate
from ollama import AsyncClient
import pytest

from ribosome.core.dom.summary import object as dom_summary
from utils import summary_asyncio as shared
from utils import summary_object_asyncio as app


class Progress:
    instances = []
    messages = []

    def __init__(self, total, **kwargs):
        self.total, self.n, self.refreshes = total, 0, 0
        self.closed = False
        self.instances.append(self)
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def refresh(self): self.refreshes += 1
    def set_postfix(self, **kwargs): assert kwargs['refresh'] is False
    def set_postfix_str(self, text, *, refresh): assert refresh is False
    @classmethod
    def write(cls, message): cls.messages.append(message)


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    Progress.instances = []
    Progress.messages = []
    monkeypatch.setattr(dom_summary, '_SUMMARY_CONCURRENCY', 2)
    monkeypatch.setattr(dom_summary, '_SUMMARY_TASK_LIMIT', 3)
    async def summarize(client, content, **kwargs):
        await asyncio.sleep(0)
        return 'OBJECT-SUMMARY'
    monkeypatch.setattr(dom_summary, 'get_summary_response_async', summarize)


def document(root, name='manual.md', source='# Title\n\n| A | B |\n| --- | --- |\n| x | y |\n'):
    root.mkdir(parents=True, exist_ok=True)
    file = root / name
    file.write_text(source, encoding='utf-8')
    return file


def run(files, client=None, **kwargs):
    options = dict(concurrency=2, progress_interval=60, progress_factory=Progress,
                   leaf_min_len=99999)
    options.update(kwargs)
    return asyncio.run(app.summarize_object_documents(
        files, client if client is not None else object(), 'offline', **options))


def test_discovery_both_corpora_and_empty_inputs(tmp_path):
    first = document(tmp_path / '4.0' / 'nested', 'manual.MD')
    second = document(tmp_path / '5.0' / 'nested')
    (first.parent / 'ignored.json').write_text('{}')
    assert app.discover_markdown(tmp_path / '4.0') == [first]
    assert app.discover_markdown(tmp_path / '5.0') == [second]
    with pytest.raises(FileNotFoundError, match='does not exist'):
        app.discover_markdown(tmp_path / 'missing')
    (tmp_path / 'empty').mkdir()
    with pytest.raises(ValueError, match='No Markdown files'):
        app.discover_markdown(tmp_path / 'empty')
    assert run([]) == []


def test_two_corpora_replace_full_trees_and_preserve_sources_and_asts(tmp_path, monkeypatch):
    files = [document(tmp_path / version) for version in ['4.0', '5.0']]
    originals = {}
    for file in files:
        ast_file = file.with_name(file.stem + '_ast.json')
        ast_file.write_bytes(b'cached AST untouched')
        output = file.with_name(file.stem + '_semantics.json')
        output.write_text('old summary')
        originals[file] = file.read_bytes(), ast_file.read_bytes()
    original_extract = dom_summary.extract_markdown_object_tree
    original_summary = dom_summary._summarize_markdown_object_async
    rows = []
    for file in files:
        rows.extend(run([file], corpus=file.parent.name,
                        checkpoint_path=file.parent / '.object-summary-checkpoint.json'))
    assert [row['corpus'] for row in rows] == ['4.0', '5.0']
    assert [row['file'] for row in rows] == [str(file) for file in files]
    for file, row in zip(files, rows):
        assert row['status'] == 'completed' and not row['resumed']
        assert row['object_count'] == row['completed_objects'] == 1
        tree = json.loads(Path(row['output']).read_text())
        assert tree['blocks'][0]['t'] == 'Section'
        assert next(dom_summary._object_nodes(tree))['object']['type'] == 'table'
        assert next(dom_summary._object_nodes(tree))['s'] in tree['summary']
        assert file.read_bytes() == originals[file][0]
        assert file.with_name(file.stem + '_ast.json').read_bytes() == originals[file][1]
        assert (file.parent / '.object-summary-checkpoint.json').exists()
    assert dom_summary.extract_markdown_object_tree is original_extract
    assert dom_summary._summarize_markdown_object_async is original_summary
    assert not app._batch_active and shared._document_id.get() is None
    assert all(bar.closed for bar in Progress.instances)
    assert all(bar.n == bar.total for bar in Progress.instances)
    assert all(bar.refreshes == 2 for bar in Progress.instances)


def test_model_language_images_and_request_metrics_are_forwarded(tmp_path, monkeypatch):
    file = document(tmp_path, source='# Title\n\n![alt](image.png)\n图 1 caption\n')
    requests = []
    async def chat(self, **kwargs):
        requests.append(kwargs)
        await asyncio.sleep(0)
        return SimpleNamespace(message=SimpleNamespace(content='summary'),
                               prompt_eval_count=2, eval_count=3)
    async def text(client, content, model, role, lang):
        assert model == 'offline' and lang == 'en'
        response = await client.chat(model=model, messages=[{'role': role, 'content': content}])
        return response.message.content
    async def image(client, image_link, model, role, lang):
        assert image_link == str(tmp_path / 'image.png') and model == 'offline' and lang == 'en'
        response = await client.chat(model=model, messages=[{'role': role, 'content': image_link}])
        return response.message.content
    monkeypatch.setattr(AsyncClient, 'chat', chat)
    monkeypatch.setattr(dom_summary, 'get_summary_response_async', text)
    monkeypatch.setattr(dom_summary, 'get_image_summary_async', image)
    async def scenario():
        client = shared.SummaryAsyncClient(host='http://offline')
        try:
            rows = await app.summarize_object_documents(
                [file], client, 'offline', lang='en', leaf_min_len=99999,
                progress_factory=Progress)
            row = rows[0]
            assert row['request_count'] == 2 and row['prompt_tokens'] == 4
            assert row['generated_tokens'] == 6 and row['request_errors'] == 0
            assert client.metrics.records[0]['document'] == str(file)
        finally:
            await client._client.aclose()
    asyncio.run(scenario())
    assert len(requests) == 2


def test_failures_are_isolated_and_keep_previous_outputs(tmp_path, monkeypatch):
    bad = document(tmp_path, 'bad.md', '# Bad\n\n- bad\n')
    empty = document(tmp_path, 'empty.md', '')
    good = document(tmp_path, 'good.md')
    for file in [bad, empty]: file.with_name(file.stem + '_semantics.json').write_text('previous')
    async def text(client, content, **kwargs):
        if '- bad' in content: raise RuntimeError('provider failed')
        return 'summary'
    monkeypatch.setattr(dom_summary, 'get_summary_response_async', text)
    rows = run([bad, empty, good])
    assert [row['status'] for row in rows] == ['failed', 'failed', 'completed']
    assert 'provider failed' in rows[0]['error']
    assert 'Empty Markdown' in rows[1]['error']
    for file in [bad, empty]: assert file.with_name(file.stem + '_semantics.json').read_text() == 'previous'
    assert all(bar.n == bar.total and bar.closed for bar in Progress.instances)


def test_checkpoint_resume_and_configuration_invalidation(tmp_path, monkeypatch):
    file = document(tmp_path)
    state = tmp_path / '.object-summary-checkpoint.json'
    options = dict(checkpoint_path=state, checkpoint_host='local')
    assert not run([file], **options)[0]['resumed']
    assert run([file], **options)[0]['resumed']
    old_checkpoint = json.loads(state.read_text())
    assert old_checkpoint['config']['settings']['object_schema_version'] == 4
    old_checkpoint['config']['settings']['object_schema_version'] = 3
    state.write_text(json.dumps(old_checkpoint))
    assert not run([file], **options)[0]['resumed']
    assert run([file], **options)[0]['resumed']
    assert not run([file], lang='en', **options)[0]['resumed']
    assert not run([file], lang='en', checkpoint_path=state, checkpoint_host='different')[0]['resumed']
    assert not run([file], resume=False, **options)[0]['resumed']
    file.write_text(file.read_text() + '\nChanged prose.\n')
    assert not run([file], **options)[0]['resumed']
    Path(run([file], **options)[0]['output']).write_text('changed output')
    assert not run([file], **options)[0]['resumed']
    checkpoint = shared.SummaryCheckpoint(state, 'other-model', 'local', True,
                                        settings={'summarizer': 'object'})
    assert checkpoint.completed(file) is None


def test_cancellation_awaits_requests_restores_hooks_and_resumes(tmp_path, monkeypatch):
    first = document(tmp_path, 'first.md')
    second = document(tmp_path, 'second.md', '# Other\n\n- block\n')
    state = tmp_path / '.object-summary-checkpoint.json'
    output = second.with_name(second.stem + '_semantics.json')
    output.write_text('previous')
    original = dom_summary._summarize_markdown_object_async
    original_extract = dom_summary.extract_markdown_object_tree
    started, active = None, set()
    async def text(client, content, **kwargs):
        key = object()
        active.add(key)
        try:
            if '- block' in content:
                started.set()
                await asyncio.Event().wait()
            return 'summary'
        finally:
            active.remove(key)
    monkeypatch.setattr(dom_summary, 'get_summary_response_async', text)
    async def scenario():
        nonlocal started
        started = asyncio.Event()
        task = asyncio.create_task(app.summarize_object_documents(
            [first, second], object(), 'offline', concurrency=1,
            checkpoint_path=state, progress_factory=Progress, leaf_min_len=99999))
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())
    assert not active and not app._batch_active
    assert output.read_text() == 'previous'
    assert dom_summary._summarize_markdown_object_async is original
    assert dom_summary.extract_markdown_object_tree is original_extract
    assert all(bar.closed for bar in Progress.instances)
    assert json.loads(state.read_text())['entries'][str(first)]['row']['status'] == 'completed'
    async def text_success(client, content, **kwargs): return 'summary'
    monkeypatch.setattr(dom_summary, 'get_summary_response_async', text_success)
    rows = run([first, second], checkpoint_path=state, concurrency=1)
    assert [row['resumed'] for row in rows] == [True, False]


def test_document_and_request_concurrency_limits(tmp_path, monkeypatch):
    files = [document(tmp_path, f'{i}.md') for i in range(6)]
    documents = peak_documents = requests = peak_requests = 0
    summarize = dom_summary.summarize_markdown_object_tree_async
    async def document_summary(*args, **kwargs):
        nonlocal documents, peak_documents
        documents += 1
        peak_documents = max(peak_documents, documents)
        try: return await summarize(*args, **kwargs)
        finally: documents -= 1
    async def text(client, content, **kwargs):
        nonlocal requests, peak_requests
        requests += 1
        peak_requests = max(peak_requests, requests)
        try:
            await asyncio.sleep(0.005)
            return 'summary'
        finally: requests -= 1
    monkeypatch.setattr(dom_summary, 'summarize_markdown_object_tree_async', document_summary)
    monkeypatch.setattr(dom_summary, 'get_summary_response_async', text)
    rows = run(files, concurrency=3)
    assert all(row['status'] == 'completed' for row in rows)
    assert peak_documents == 3 and peak_requests == 2
    assert documents == requests == 0


@pytest.mark.parametrize('kwargs', [{'concurrency': 0}, {'concurrency': True},
                                   {'progress_interval': 0}, {'progress_interval': float('nan')}])
def test_invalid_batch_settings(kwargs):
    with pytest.raises(ValueError): run([], **kwargs)


def test_preflight_missing_model_and_provider_failure():
    async def list_models(): return SimpleNamespace(models=[SimpleNamespace(model='installed')])
    client = SimpleNamespace(list=list_models)
    assert asyncio.run(app.check_model(client, 'installed', 'local')) == 'installed'
    with pytest.raises(RuntimeError, match='unavailable'):
        asyncio.run(app.check_model(client, 'missing', 'local'))
    async def fail(): raise ConnectionError('offline')
    with pytest.raises(RuntimeError, match='Cannot list Ollama models at local'):
        asyncio.run(app.check_model(SimpleNamespace(list=fail), 'installed', 'local'))


def test_limits_validate_selected_object_module(monkeypatch):
    for key in ['RIBOSOME_DOCUMENT_CONCURRENCY', 'RIBOSOME_SUMMARY_CONCURRENCY', 'RIBOSOME_SUMMARY_TASK_LIMIT']:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(sys.modules, 'ribosome.core.dom.summary.object',
                        SimpleNamespace(_SUMMARY_CONCURRENCY=8, _SUMMARY_TASK_LIMIT=32))
    assert list(shared.configure_summary_limits(summary_module='object').values()) == [4, 8, 32]
    monkeypatch.setenv('RIBOSOME_SUMMARY_CONCURRENCY', '2')
    with pytest.raises(RuntimeError, match='Restart the kernel'):
        shared.configure_summary_limits(summary_module='object')
    with pytest.raises(ValueError, match='summary_module'):
        shared.configure_summary_limits(summary_module='invalid')


def test_notebook_validates_compiles_and_uses_both_corpora():
    notebook = read('nbs/05.application.summary.object.ipynb', as_version=4)
    validate(notebook)
    for cell in notebook.cells:
        if cell.cell_type == 'code':
            # Saved execution counts and outputs are valid after using the notebook.
            compile(cell.source, '<object-batch-notebook>', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    config = notebook.cells[2].source
    assert config.index('configure_summary_limits(summary_module="object")') < config.index('from utils.summary_object_asyncio')
    assert 'md.json.semantics.coding.4.0' in config and 'md.json.semantics.coding.5.0' in config
    assert '.object-summary-checkpoint.json' in config
    assert 'await summarize_object_documents(' in notebook.cells[6].source
    assert 'DOMClass' not in config and 'chromadb' not in config


def test_notebook_configuration_uses_project_env_and_api_key(tmp_path, monkeypatch):
    (tmp_path / 'pyproject.toml').write_text('')
    env = tmp_path / '.env'
    env.write_text('OLLAMA_HOST=http://configured:11434\nOLLAMA_SUMMARY_MODEL=vision\nOLLAMA_API_KEY=test-key\n')
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path))
    captured = {}
    def client(**kwargs): captured.update(kwargs); return object()
    def limits(**kwargs):
        assert kwargs == {'summary_module': 'object'}
        return {'RIBOSOME_DOCUMENT_CONCURRENCY': 4}
    monkeypatch.setattr(shared, 'SummaryAsyncClient', client)
    monkeypatch.setattr(shared, 'configure_summary_limits', limits)
    # Keep load_dotenv's environment changes inside this test.
    for key in ['OLLAMA_HOST', 'OLLAMA_SUMMARY_MODEL', 'OLLAMA_API_KEY']:
        monkeypatch.setenv(key, 'stale')
    notebook = read('nbs/05.application.summary.object.ipynb', as_version=4)
    namespace = {}
    exec(compile(notebook.cells[2].source, '<object-batch-config>', 'exec'), namespace)
    assert captured == {'host': 'http://configured:11434', 'headers': {'Authorization': 'Bearer test-key'}}
    assert namespace['SUMMARY_MODEL'] == 'vision'
    assert [p.name for p in namespace['INPUT_DIRS']] == ['md.json.semantics.coding.4.0', 'md.json.semantics.coding.5.0']
    env.write_text('OLLAMA_HOST=http://configured:11434\n')
    with pytest.raises(ValueError, match='OLLAMA_SUMMARY_MODEL'):
        exec(compile(notebook.cells[2].source, '<object-batch-config>', 'exec'), {})
    env.unlink()
    with pytest.raises(FileNotFoundError, match='Missing configuration'):
        exec(compile(notebook.cells[2].source, '<object-batch-config>', 'exec'), {})
