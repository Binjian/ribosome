"""Offline coverage for two-corpus object summarization and its notebook."""
import ast
import asyncio
from collections import Counter
from copy import deepcopy
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
    def set_postfix(self, **kwargs):
        assert kwargs['refresh'] is False
        self.status = kwargs
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
    assert old_checkpoint['config']['settings']['object_schema_version'] == 5
    old_checkpoint['config']['settings']['object_schema_version'] = 4
    state.write_text(json.dumps(old_checkpoint))
    assert run([file], **options)[0]['resumed']
    assert run([file], **options)[0]['resumed']
    assert not run([file], lang='en', **options)[0]['resumed']
    assert run([file], lang='en', checkpoint_path=state, checkpoint_host='different')[0]['resumed']
    assert not run([file], resume=False, **options)[0]['resumed']
    file.write_text(file.read_text() + '\nChanged prose.\n')
    assert not run([file], **options)[0]['resumed']
    Path(run([file], **options)[0]['output']).write_text('changed output')
    assert not run([file], **options)[0]['resumed']
    checkpoint = shared.SummaryCheckpoint(state, 'other-model', 'local', True,
                                        settings={'summarizer': 'object'})
    assert checkpoint.completed(file) is None


def test_added_recursion_fills_completed_documents_and_summarizes_pending_ones(tmp_path, monkeypatch):
    first = document(tmp_path, 'completed.md', '- parent\n  - child\n')
    pending = document(tmp_path, 'pending.md', '- next\n  - nested\n')
    state = tmp_path / '.object-summary-checkpoint.json'
    options = dict(checkpoint_path=state, checkpoint_host='local')
    assert run([first], **options)[0]['status'] == 'completed'
    saved = json.loads(state.read_text())
    saved['config']['settings']['object_schema_version'] = 4
    output = first.with_name('completed_semantics.json')
    tree = json.loads(output.read_text())
    wrapper, = dom_summary._object_nodes(tree)
    wrapper['object'].pop('children')
    wrapper.pop('subnodes')
    output.write_text(json.dumps(tree))
    saved['entries'][str(first)]['signature'] = shared.SummaryCheckpoint.signature(first)
    state.write_text(json.dumps(saved))
    previous = output.read_bytes()
    requests = []
    async def summarize(client, content, **kwargs):
        if shared._document_id.get() == str(first):
            assert content.startswith('Summarize this list item')
        requests.append(content)
        return 'new summary'
    monkeypatch.setattr(dom_summary, 'get_summary_response_async', summarize)
    rows = run([first, pending], **options)
    assert [row['resumed'] for row in rows] == [False, False]
    assert rows[0]['incremental'] and rows[0]['filled_summaries'] == 2
    assert all(row['status'] == 'completed' for row in rows)
    assert output.read_bytes() != previous
    upgraded = json.loads(output.read_text())
    assert upgraded['summary'] == tree['summary']
    assert next(dom_summary._object_nodes(upgraded))['s'] == wrapper['s']
    assert len(requests) == 5  # Two missing items, two pending items and the pending list.
    result = json.loads(pending.with_name('pending_semantics.json').read_text())
    wrapper, = dom_summary._object_nodes(result)
    assert wrapper['subnodes'][0]['subnodes'][0]['object']['type'] == 'list_item'
    assert all(row['resumed'] for row in run([first, pending], **options))


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
    partial = app._partial_path(second)
    assert partial.is_file()
    saved_tree = json.loads(partial.read_text())['tree']
    assert list(dom_summary._object_nodes(saved_tree))
    async def text_success(client, content, **kwargs): return 'summary'
    monkeypatch.setattr(dom_summary, 'get_summary_response_async', text_success)
    rows = run([first, second], checkpoint_path=state, concurrency=1)
    # Both completed-document and interrupted-document checkpoints are resumed.
    assert [row['resumed'] for row in rows] == [True, True]
    assert all(row['status'] == 'completed' for row in rows)
    assert not partial.exists()
    assert json.loads(output.read_text())['summary']


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
    monkeypatch.setenv('RIBOSOME_DOCUMENT_CONCURRENCY', '6')
    assert shared.configure_summary_limits(summary_module='object')['RIBOSOME_DOCUMENT_CONCURRENCY'] == 6
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
    assert 'code-docs.4.0' in config and 'code-docs.5.0' in config
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
        return {'RIBOSOME_DOCUMENT_CONCURRENCY': 4, 'RIBOSOME_SUMMARY_CONCURRENCY': 8}
    from utils import summary_ollama_pool
    monkeypatch.setattr(summary_ollama_pool, 'OllamaSummaryPool', client)
    monkeypatch.setattr(shared, 'configure_summary_limits', limits)
    # Keep load_dotenv's environment changes inside this test.
    for key in ['OLLAMA_HOST', 'OLLAMA_SUMMARY_MODEL', 'OLLAMA_API_KEY']:
        monkeypatch.setenv(key, 'stale')
    notebook = read('nbs/05.application.summary.object.ipynb', as_version=4)
    namespace = {}
    exec(compile(notebook.cells[2].source, '<object-batch-config>', 'exec'), namespace)
    assert captured == {'hosts': ['http://127.0.0.1:11434', 'http://configured:11434'],
                        'concurrency_per_server': 4, 'headers': {'Authorization': 'Bearer test-key'},
                        'request_timeout': 300.0, 'max_tokens': 2048, 'think': False}
    assert namespace['SUMMARY_MODEL'] == 'vision'
    assert namespace['INPUT_DIRS'] == [
        tmp_path / 'assets' / 'code-docs.4.0' / '.md',
        tmp_path / 'assets' / 'code-docs.5.0' / '.md',
    ]
    assert namespace['IGNORED_IMAGES_DIR'] == tmp_path / 'assets' / 'images_ignored'
    assert namespace['PREVIEW_OUTPUTS'] == 3
    assert namespace['RESUME'] is True
    assert namespace['os'].environ['PROJ_ROOT'] == str(tmp_path)
    env.write_text('OLLAMA_HOST=http://configured:11434\n')
    with pytest.raises(ValueError, match='OLLAMA_SUMMARY_MODEL'):
        exec(compile(notebook.cells[2].source, '<object-batch-config>', 'exec'), {})
    env.unlink()
    with pytest.raises(FileNotFoundError, match='Missing configuration'):
        exec(compile(notebook.cells[2].source, '<object-batch-config>', 'exec'), {})


def test_saved_output_preview_includes_nested_rows_and_items_and_accepts_only_intentional_empty_summaries(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path))
    references = tmp_path / 'assets' / 'images_ignored'
    references.mkdir(parents=True)
    Image.new('RGB', (40, 20), 'red').save(references / 'logo.png')
    (tmp_path / 'logo.png').write_bytes((references / 'logo.png').read_bytes())
    file = document(tmp_path, source=(
        '# Manual\n\n![ignored](logo.png)\n图 1 Logo\n\n'
        '| Header |\n| --- |\n| value |\n\n'
        '<table><tr><td rowspan="3">A</td><td rowspan="2">B</td><td>one</td></tr>'
        '<tr><td>two</td></tr><tr><td>three</td></tr></table>\n\n'
        'Steps:\n\n3. First\n   - Nested\n4. Second\n'))
    tree = asyncio.run(dom_summary.summarize_markdown_object_tree_async(
        file, object(), 'offline', leaf_min_len=99999))
    notebook = read('nbs/05.application.summary.object.ipynb', as_version=4)
    source = next(cell.source for cell in notebook.cells
                  if cell.cell_type == 'code' and 'def summary_records(' in cell.source)
    scope = {'results': [], 'PREVIEW_OUTPUTS': 0}
    exec(compile(source, '<object-output-preview>', 'exec'), scope)
    records = scope['summary_records'](tree)
    assert Counter(record['type'] for record in records) == {
        'figure': 1, 'table': 2, 'table_row': 5, 'table_row_group': 2,
        'list': 1, 'list_item': 3}
    ignored, = [record for record in records if record['status'] == 'ignored']
    assert ignored['summary'] == '' and ignored['ignored_images'] == 1
    assert max(record['depth'] for record in records) == 3
    assert all(record['summary'].strip() for record in records if record['status'] == 'summarized')
    assert [record['is_header'] for record in records if record['type'] == 'table_row'].count(True) == 1
    items = [record for record in records if record['type'] == 'list_item']
    assert [record['ordinal'] for record in items] == [3, None, 4]
    assert [record['depth'] for record in items] == [1, 2, 1]
    assert items[1]['parent_id'] == items[0]['id']
    invalid = deepcopy(tree)
    figure = next(dom_summary._object_nodes(invalid))
    figure['object'].pop('summary_ignored')
    with pytest.raises(ValueError, match='Invalid summary for figure'):
        scope['summary_records'](invalid)
    invalid = deepcopy(tree)
    table = next(node for node in dom_summary._object_nodes(invalid) if node['object']['type'] == 'table')
    table['subnodes'] = []
    with pytest.raises(ValueError, match='Object hierarchy mismatch'):
        scope['summary_records'](invalid)
    invalid = deepcopy(tree)
    list_node = next(node for node in dom_summary._object_nodes(invalid) if node['object']['type'] == 'list')
    list_node['subnodes'][0]['subnodes'] = []
    with pytest.raises(ValueError, match='Object hierarchy mismatch'):
        scope['summary_records'](invalid)


def test_batch_progress_reports_actual_durations_and_excludes_resumed(tmp_path):
    file = document(tmp_path)
    options = dict(checkpoint_path=tmp_path / 'checkpoint.json')
    row = run([file], **options)[0]
    documents, objects = Progress.instances[:2]
    assert documents.status['mean_doc'] == f"{row['elapsed_seconds']:.1f}s"
    assert documents.status['resumed'] == 0
    assert objects.n == objects.total == 0
    next_batch = len(Progress.instances)
    run([file], **options)
    documents = Progress.instances[next_batch]
    assert documents.status['resumed'] == 1
    assert 'mean_doc' not in documents.status


def test_generation_settings_invalidate_checkpoint(tmp_path):
    file = document(tmp_path)
    async def scenario():
        client = shared.SummaryAsyncClient(host='http://offline', max_tokens=42, think=False)
        options = dict(checkpoint_path=tmp_path / 'checkpoint.json',
                       progress_factory=Progress, leaf_min_len=99999)
        try:
            async def batch():
                return (await app.summarize_object_documents([file], client, 'offline', **options))[0]
            assert not (await batch())['resumed']
            assert (await batch())['resumed']
            client.max_tokens = 84
            assert not (await batch())['resumed']
            client.think = True
            assert not (await batch())['resumed']
        finally:
            await client._client.aclose()
    asyncio.run(scenario())


def test_short_document_publishes_and_reuses_worker_while_long_documents_run(tmp_path, monkeypatch):
    files = [document(tmp_path, f'long{i}.md', '- long\n') for i in range(3)]
    short = document(tmp_path, 'short.md', '- short\n')
    following = document(tmp_path, 'following.md', '- following\n')
    files += [short, following]
    checkpoint = tmp_path / 'checkpoint.json'
    monkeypatch.setattr(dom_summary, '_SUMMARY_CONCURRENCY', 8)
    original_semaphore = dom_summary._get_summary_semaphore

    async def scenario():
        release_long, release_short = asyncio.Event(), asyncio.Event()
        final_visible, following_started = asyncio.Event(), asyncio.Event()
        blocked = set()
        class ObservedProgress(Progress):
            def set_postfix_str(self, text, *, refresh):
                super().set_postfix_str(text, refresh=refresh)
                if 'short.md: objects 1/1, final document summary' in text:
                    assert 'running=1' in text
                    final_visible.set()
        async def text(client, content, **kwargs):
            name = Path(shared._document_id.get()).name
            if name.startswith('long'):
                blocked.add(name)
                await release_long.wait()
            elif name == 'short.md' and dom_summary._summary_stage.get() == 'final document summary':
                await release_short.wait()
            elif name == 'following.md':
                following_started.set()
            return 'summary'
        monkeypatch.setattr(dom_summary, 'get_summary_response_async', text)
        task = asyncio.create_task(app.summarize_object_documents(
            files, object(), 'offline', concurrency=4, leaf_min_len=1,
            checkpoint_path=checkpoint, progress_factory=ObservedProgress, progress_interval=0.001))
        try:
            await asyncio.wait_for(final_visible.wait(), 3)
            assert len(blocked) == 3
            assert not short.with_name('short_semantics.json').exists()
            release_short.set()
            await asyncio.wait_for(following_started.wait(), 3)
            assert not task.done()  # Three long documents are still blocked.
            assert short.with_name('short_semantics.json').exists()
            entries = json.loads(checkpoint.read_text())['entries']
            assert entries[str(short)]['row']['status'] == 'completed'
            assert not any(str(file) in entries for file in files[:3])
            assert any(f'COMPLETED {short}' in message for message in Progress.messages)
        finally:
            release_short.set()
            release_long.set()
            rows = await asyncio.wait_for(task, 3)
        assert [row['file'] for row in rows] == [str(file) for file in files]
        assert all(row['status'] == 'completed' for row in rows)
    asyncio.run(scenario())
    assert dom_summary._get_summary_semaphore is original_semaphore


def test_queue_progress_and_cancellation_release_shared_slots(tmp_path, monkeypatch):
    files = [document(tmp_path, f'{i}.md', '- item\n') for i in range(2)]
    monkeypatch.setattr(dom_summary, '_SUMMARY_CONCURRENCY', 1)
    original_semaphore = dom_summary._get_summary_semaphore
    async def scenario():
        queued = asyncio.Event()
        class ObservedProgress(Progress):
            def set_postfix_str(self, text, *, refresh):
                super().set_postfix_str(text, refresh=refresh)
                if 'queued=1' in text and 'running=1' in text:
                    queued.set()
        async def text(*args, **kwargs):
            await asyncio.Event().wait()
        monkeypatch.setattr(dom_summary, 'get_summary_response_async', text)
        task = asyncio.create_task(app.summarize_object_documents(
            files, object(), 'offline', concurrency=2, leaf_min_len=1,
            progress_factory=ObservedProgress, progress_interval=0.001))
        try:
            await asyncio.wait_for(queued.wait(), 3)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert dom_summary._get_summary_semaphore is original_semaphore
        semaphore = original_semaphore()
        await asyncio.wait_for(semaphore.acquire(), 1)
        semaphore.release()
        assert not app._batch_active
    asyncio.run(scenario())
