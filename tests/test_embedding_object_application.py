"""Offline checks for the operational object-embedding notebook."""
import ast
import asyncio
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from nbformat import read, validate

from ribosome.core.dom.embedding.object import IndexingResult, ObjectEmbeddingIndex
from test_object_embedding import Provider, ordered_prose_tree, record


NOTEBOOK = Path('nbs/05.application.embedding.object.ipynb')


def code_with(marker):
    return next(cell.source for cell in read(NOTEBOOK, as_version=4).cells
                if cell.cell_type == 'code' and marker in cell.source)


def helpers():
    namespace = {}
    exec(compile(code_with('def discover_semantics'), str(NOTEBOOK), 'exec'), namespace)
    return namespace


def semantics(folder, name, summary='Document', *, suffix='_semantics.json'):
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / f'{name}{suffix}'
    file.write_text(json.dumps({'summary': summary, 'objects': [record(1, 'figure', 'Object')]}))
    return file


def test_notebook_is_valid_offline_and_all_cells_compile():
    nb = read(NOTEBOOK, as_version=4)
    validate(nb)
    for cell in nb.cells:
        if cell.cell_type != 'code':
            continue
        compile(cell.source, str(NOTEBOOK), 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        # Saved outputs, including the index comparison, are valid notebook content.
        if 'def discover_semantics' not in cell.source:
            assert '# | notest' in cell.source


@pytest.mark.parametrize('mode', ['object', 'task', 'remaining'])
@pytest.mark.parametrize('server_mode,expected_hosts,host_directory', [
    ('16', ['http://172.27.74.16:11434'], '172.27.74.16-11434'),
    ('49', ['http://172.27.74.49:11434'], '172.27.74.49-11434'),
    ('both', ['http://172.27.74.16:11434', 'http://172.27.74.49:11434'],
     '172.27.74.16-11434--172.27.74.49-11434'),
])
def test_configuration_reads_env_with_overrides_without_opening_clients(
        tmp_path, monkeypatch, mode, server_mode, expected_hosts, host_directory):
    (tmp_path / 'pyproject.toml').write_text('')
    (tmp_path / '.env').write_text('OLLAMA_EMBED_HOSTS=http://172.27.74.49:11434,http://from-file:11434\nOLLAMA_EMBED_MODEL=file-model\nOLLAMA_API_KEY=fake-secret\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    import ollama
    monkeypatch.setattr(ollama, 'AsyncClient', lambda **kw: pytest.fail('Configuration opened a provider client'))
    monkeypatch.setattr(chromadb, 'PersistentClient', lambda **kw: pytest.fail('Configuration opened an index'))
    with patch.dict(os.environ, {'PROJ_ROOT': str(tmp_path), 'OLLAMA_EMBED_MODEL': 'override-model'}, clear=True):
        namespace = {}
        source = code_with('PROJ_ROOT = resolve_project_root()').replace('EMBEDDING_MODE = "remaining"', f'EMBEDDING_MODE = "{mode}"')
        source = source.replace('OLLAMA_SERVER_MODE = "both"', f'OLLAMA_SERVER_MODE = "{server_mode}"')
        exec(compile(source, str(NOTEBOOK), 'exec'), namespace)
        assert namespace['EMBED_MODEL'] == 'override-model'
        assert namespace['OLLAMA_SERVER_MODE'] == server_mode
        assert namespace['OLLAMA_HOSTS'] == expected_hosts
        assert namespace['EMBEDDING_CONCURRENCY'] == 4 * len(expected_hosts)
        assert namespace['headers'] == {'Authorization': 'Bearer fake-secret'}
        assert namespace['EMBEDDING_CONFIG'] == {'provider': 'ollama-pool', 'hosts': expected_hosts}
        expected_inputs = ([tmp_path / 'assets/md.json.semantics.remaining'] if mode == 'remaining' else
                           [tmp_path / 'assets/code-docs.4.0/.md', tmp_path / 'assets/code-docs.5.0/.md'])
        assert namespace['INPUT_DIRS'] == expected_inputs
        use_tasks = mode == 'task'
        directory = 'task_embeddings' if use_tasks else 'object_embeddings'
        index_root = tmp_path / 'db' / directory
        if mode == 'remaining':
            index_root /= 'md.json.semantics.remaining'
        assert namespace['EMBEDDING_MODE'] == mode
        assert namespace['USE_TASKS'] is use_tasks
        assert namespace['INPUT_PATTERN'] == ('*_tasks.json' if use_tasks else '*_semantics.json')
        assert namespace['COLLECTION_NAME'] == ('task_embeddings' if use_tasks else 'object_summaries')
        assert namespace['CATALOG_PATH'] == index_root / f'override-model-{host_directory}/catalog.sqlite3'
        assert not namespace['INDEX_DIR'].exists()


def test_discovery_includes_nested_files_in_root_order_and_deduplicates(tmp_path):
    discover = helpers()['discover_semantics']
    second = tmp_path / 'second'
    first = tmp_path / 'first'
    a = semantics(first, 'a')
    b = semantics(first / 'nested', 'b')
    c = semantics(second, 'c')
    (first / 'ignored_summary-progress.json').write_text('{}')
    corpora = discover([second, first, first / 'nested', second])
    assert corpora == [(second, [c]), (first, [a, b]), (first / 'nested', []), (second, [])]


def test_task_discovery_selects_only_task_outputs_and_deduplicates(tmp_path):
    discover = helpers()['discover_semantics']
    first = semantics(tmp_path, 'a', suffix='_tasks.json')
    second = semantics(tmp_path / 'nested', 'b', suffix='_tasks.json')
    semantics(tmp_path, 'a')
    (tmp_path / 'ignored_summary-progress.json').write_text('{}')
    assert discover([tmp_path, tmp_path / 'nested'], pattern='*_tasks.json') == [
        (tmp_path, [first, second]), (tmp_path / 'nested', []),
    ]


def test_task_discovery_does_not_fall_back_to_semantics(tmp_path):
    semantics(tmp_path, 'a')
    with pytest.raises(ValueError, match=r'No \*_tasks\.json'):
        helpers()['discover_semantics'](tmp_path, pattern='*_tasks.json')


@pytest.mark.parametrize('mode', ['missing', 'empty', 'no-folders'])
def test_discovery_rejects_invalid_inputs(tmp_path, mode):
    discover = helpers()['discover_semantics']
    if mode == 'empty':
        with pytest.raises(ValueError, match='No .*semantics'):
            discover([tmp_path])
    elif mode == 'no-folders':
        with pytest.raises(ValueError, match='at least one'):
            discover([])
    else:
        with pytest.raises(FileNotFoundError, match='does not exist'):
            discover([tmp_path / 'missing'])


def test_batch_indexes_all_valid_files_reports_failures_and_resumes(tmp_path):
    namespace = helpers()
    folder = tmp_path / 'corpus'
    first = semantics(folder, 'a')
    second = semantics(folder / 'nested', 'b')
    invalid = folder / 'broken_semantics.json'
    invalid.write_text('{bad')
    before = {file: file.read_bytes() for file in (first, second, invalid)}
    corpora = namespace['discover_semantics']([folder, folder / 'nested'])
    client = chromadb.EphemeralClient()
    collection = client.create_collection('application-offline', metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    provider = Provider()
    try:
        with ObjectEmbeddingIndex(tmp_path / 'catalog.sqlite3', collection=collection, llm_client=provider) as index:
            rows = []
            asyncio.run(namespace['embed_corpora'](corpora, index, rows, progress=False, progress_interval=0.01))
            assert [row['status'] for row in rows] == ['indexed', 'failed', 'indexed']
            assert len(rows) == 3
            assert all(row['corpus'] == str(folder) for row in rows)
            assert all(row['elapsed_seconds'] >= 0 for row in rows)
            assert 'JSONDecodeError' in rows[1]['error']
            request_count = len(provider.calls)
            repeated = []
            asyncio.run(namespace['embed_corpora'](corpora, index, repeated, progress=False))
            assert [row['status'] for row in repeated] == ['unchanged', 'failed', 'unchanged']
            assert len(provider.calls) == request_count
            preview_code = code_with('def catalog_preview').split('\npreview_documents =')[0]
            exec(compile(preview_code, str(NOTEBOOK), 'exec'), namespace)
            preview = namespace['catalog_preview'](index, rows[0]['document_id'])
            assert [row['type'] for row in preview] == ['document', 'figure']
            assert preview[1]['parent_id'] == preview[0]['id']
        assert {file: file.read_bytes() for file in before} == before
    finally:
        client.delete_collection(collection.name)


def test_batch_indexes_legacy_ordered_lists_and_reuses_completed_vectors(tmp_path):
    namespace = helpers()
    source = tmp_path / 'legacy_semantics.json'
    source.write_text(json.dumps(ordered_prose_tree('21'), ensure_ascii=False))
    before = source.read_bytes()
    corpora = namespace['discover_semantics']([tmp_path])
    client = chromadb.EphemeralClient()
    collection = client.create_collection('application-legacy-ordered-list',
        metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    provider = Provider()
    try:
        with ObjectEmbeddingIndex(tmp_path / 'catalog.sqlite3', collection=collection,
                                  llm_client=provider) as index:
            rows = []
            asyncio.run(namespace['embed_corpora'](corpora, index, rows, progress=False))
            assert [row['status'] for row in rows] == ['indexed']
            requested = [summary for _, summary in provider.calls]
            assert any('21. First raw' in summary and '22. Second raw' in summary for summary in requested)
            generation = rows[0]['generation_id']
            count = len(provider.calls)
            repeated = []
            asyncio.run(namespace['embed_corpora'](corpora, index, repeated, progress=False))
            assert [row['status'] for row in repeated] == ['unchanged']
            assert repeated[0]['generation_id'] == generation
            assert len(provider.calls) == count
            assert source.read_bytes() == before
    finally:
        client.delete_collection(collection.name)


def test_cancellation_awaits_active_file_and_retains_completed_reports(tmp_path):
    namespace = helpers()
    first, second = tmp_path / 'a_semantics.json', tmp_path / 'b_semantics.json'
    started = asyncio.Event()
    cancelled = []

    class SlowIndex:
        async def index_file(self, file):
            if file == first:
                return IndexingResult(str(file), 'indexed', document_id='a')
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.append(file)

    rows = []

    async def check():
        task = asyncio.create_task(namespace['embed_corpora']([(tmp_path, [first, second])], SlowIndex(), rows,
                                   progress=False, progress_interval=0.01))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(check())
    assert cancelled == [second]
    assert [row['semantics_file'] for row in rows] == [str(first)]


@pytest.mark.parametrize('failure', [False, True])
def test_preflight_checks_pool_and_closes_on_error(tmp_path, failure):
    namespace = helpers()
    semantics(tmp_path, 'a', suffix='_tasks.json')
    calls = []

    class Pool:
        def __init__(self, **kwargs):
            self.hosts = kwargs['hosts']
            calls.append(('setup', kwargs))

        async def check_models(self, model, *, require_all=False):
            calls.append(('check_models', (model, require_all)))
            if failure:
                raise RuntimeError('remote model missing')
            return model

        def server_snapshots(self):
            return [dict(host=host, health='healthy') for host in self.hosts]

        async def aclose(self):
            calls.append(('close', None))

    hosts = ['http://172.27.74.49:11434', 'http://172.27.74.16:11434']
    namespace.update(INPUT_DIRS=[tmp_path], OllamaEmbeddingPool=Pool,
                     INPUT_PATTERN='*_tasks.json',
                     OLLAMA_HOSTS=hosts, SERVER_CONCURRENCY=1, headers={},
                     REQUEST_TIMEOUT=300, REQUEST_RETRIES=2, QUARANTINE_SECONDS=30,
                     HEALTH_TIMEOUT=60, EMBED_MODEL='qwen3-embedding:8b')
    code = compile(code_with('await ollama_client.check_models'), str(NOTEBOOK), 'exec',
                   flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    if failure:
        with pytest.raises(RuntimeError, match='remote model missing'):
            asyncio.run(eval(code, namespace))
        assert calls[-1] == ('close', None)
    else:
        asyncio.run(eval(code, namespace))
        assert calls[-1] == ('check_models', ('qwen3-embedding:8b', True))
    assert calls[0] == ('setup', {'hosts': hosts, 'concurrency_per_server': 1, 'headers': {}, 'request_timeout': 300,
                                'request_retries': 2, 'quarantine_seconds': 30, 'health_timeout': 60})
    assert not (tmp_path / 'db').exists()


def test_dotenv_hosts_do_not_change_default_remaining_configuration(tmp_path, monkeypatch):
    (tmp_path / 'pyproject.toml').write_text('')
    (tmp_path / '.env').write_text('OLLAMA_EMBED_HOSTS=http://172.27.74.49:11434,http://172.27.74.16:11434\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    with patch.dict(os.environ, {'PROJ_ROOT': str(tmp_path)}, clear=True):
        app = {}
        exec(compile(code_with('PROJ_ROOT = resolve_project_root()'), str(NOTEBOOK), 'exec'), app)
        assert app['EMBED_MODEL'] == 'qwen3-embedding:8b'
        assert app['OLLAMA_SERVER_MODE'] == 'both'
        assert app['OLLAMA_HOSTS'] == ['http://172.27.74.16:11434', 'http://172.27.74.49:11434']
        assert app['EMBEDDING_CONCURRENCY'] == 8
        assert app['REQUEST_RETRIES'] == 2
        assert app['QUARANTINE_SECONDS'] == 30
        assert app['HEALTH_TIMEOUT'] == 60
        assert app['EMBEDDING_MODE'] == 'remaining'
        assert app['USE_TASKS'] is False
        assert app['INPUT_PATTERN'] == '*_semantics.json'
        assert app['COLLECTION_NAME'] == 'object_summaries'
        assert app['INDEX_DIR'] == tmp_path / 'db/object_embeddings/md.json.semantics.remaining/qwen3-embedding-8b-172.27.74.16-11434--172.27.74.49-11434'
        assert app['FORCE_REBUILD'] is False
        assert not app['INDEX_DIR'].exists()


def test_modes_create_separate_collections_and_catalogs_and_resume(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('')
    folder = tmp_path / 'corpus'
    folder.mkdir()
    files = []
    for suffix, kind in [('_semantics.json', 'table'), ('_tasks.json', 'task')]:
        file = folder / f'manual{suffix}'
        file.write_text(json.dumps({'summary': 'Document', 'objects': [
            record(1, kind, 'Program'), record(2, 'figure', 'Figure'),
        ]}))
        files.append(file)
    original_sources = {file: file.read_bytes() for file in files}
    setup = code_with('object_index = ObjectEmbeddingIndex(').split('\nresults = []', 1)[0]
    snapshots = []
    with patch.dict(os.environ, {'PROJ_ROOT': str(tmp_path)}, clear=True):
        for mode, kind, name in [('object', 'table', 'object_summaries'),
                                 ('task', 'task', 'task_embeddings'),
                                 ('remaining', 'table', 'object_summaries')]:
            app = {}
            config = code_with('PROJ_ROOT = resolve_project_root()').replace('EMBEDDING_MODE = "remaining"', f'EMBEDDING_MODE = "{mode}"')
            exec(compile(config, str(NOTEBOOK), 'exec'), app)
            exec(compile(code_with('def discover_semantics'), str(NOTEBOOK), 'exec'), app)
            provider = Provider()
            provider.hosts = app['OLLAMA_HOSTS']
            app['ollama_client'] = provider
            corpora = app['discover_semantics']([folder], pattern=app['INPUT_PATTERN'])
            assert len(corpora[0][1]) == 1
            assert corpora[0][1][0].name.endswith('_tasks.json' if mode == 'task' else '_semantics.json')
            exec(compile(setup, str(NOTEBOOK), 'exec'), app)
            index = app['object_index']
            try:
                assert index.collection.name == name
                assert index.catalog_path == app['CATALOG_PATH']
                assert not provider.calls
                rows = []
                asyncio.run(app['embed_corpora'](corpora, index, rows, progress=False))
                assert rows[0]['status'] == 'indexed'
                document = index.get_object(rows[0]['document_id'])
                assert [index.get_object(identifier).type for identifier in document.children_ids] == [kind, 'figure']
                assert index.collection.count() == 3
                calls = len(provider.calls)
                index.close()
                exec(compile(setup, str(NOTEBOOK), 'exec'), app)
                index = app['object_index']
                repeated = []
                asyncio.run(app['embed_corpora'](corpora, index, repeated, progress=False))
                assert repeated[0]['status'] == 'unchanged'
                assert len(provider.calls) == calls
            finally:
                index.close()
            # Creating another mode must not write any previous mode's catalog.
            for catalog, digest, collection in snapshots:
                assert hashlib.sha256(catalog.read_bytes()).hexdigest() == digest
                assert collection.count() == 3
            snapshots.append((index.catalog_path, hashlib.sha256(index.catalog_path.read_bytes()).hexdigest(), index.collection))
    assert len({catalog for catalog, _, _ in snapshots}) == 3
    assert len({collection.id for _, _, collection in snapshots}) == 3
    assert {file: file.read_bytes() for file in original_sources} == original_sources


def test_notebook_preflight_and_indexing_send_summaries_to_both_servers(tmp_path, monkeypatch):
    from ollama import AsyncClient
    from utils.embedding.embedding_ollama_pool import HEALTH_CHECK_TEXT

    (tmp_path / 'pyproject.toml').write_text('')
    semantics(tmp_path / 'assets/md.json.semantics.remaining', 'a')
    requests = []

    async def show(self, model):
        return {'capabilities': ['embedding']}

    async def embed(self, *, model, input):
        if input != HEALTH_CHECK_TEXT:
            requests.append((str(self._client.base_url).rstrip('/'), input))
        await asyncio.sleep(0)
        return {'embeddings': [[1.0, 2.0]], 'model': model}

    monkeypatch.setattr(AsyncClient, 'show', show)
    monkeypatch.setattr(AsyncClient, 'embed', embed)
    app = helpers()
    with patch.dict(os.environ, {'PROJ_ROOT': str(tmp_path)}, clear=True):
        exec(compile(code_with('PROJ_ROOT = resolve_project_root()'), str(NOTEBOOK), 'exec'), app)
    preflight = compile(code_with('await ollama_client.check_models'), str(NOTEBOOK), 'exec',
                        flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    setup = code_with('object_index = ObjectEmbeddingIndex(').split('\nresults = []', 1)[0]

    async def scenario():
        await eval(preflight, app)
        try:
            exec(compile(setup, str(NOTEBOOK), 'exec'), app)
            index = app['object_index']
            try:
                rows = []
                await app['embed_corpora'](app['corpora'], index, rows, progress=False)
                assert rows[0]['status'] == 'indexed'
                assert {host for host, _ in requests} == set(app['OLLAMA_HOSTS'])
                assert {text for _, text in requests} == {'Document', 'Object'}
                assert len(requests) == 2
                repeated = []
                await app['embed_corpora'](app['corpora'], index, repeated, progress=False)
                assert repeated[0]['status'] == 'unchanged'
                assert len(requests) == 2
            finally:
                index.close()
        finally:
            await app['ollama_client'].aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize('stale', ['selector', 'client', 'identity', 'concurrency'])
def test_indexing_rejects_stale_single_server_runtime_before_opening_database(tmp_path, monkeypatch, stale):
    (tmp_path / 'pyproject.toml').write_text('')
    app = {}
    with patch.dict(os.environ, {'PROJ_ROOT': str(tmp_path)}, clear=True):
        exec(compile(code_with('PROJ_ROOT = resolve_project_root()'), str(NOTEBOOK), 'exec'), app)
    provider = Provider()
    provider.hosts = list(app['OLLAMA_HOSTS'])
    app['ollama_client'] = provider
    if stale == 'selector':
        del app['OLLAMA_SERVER_MODE']
    elif stale == 'client':
        provider.hosts = ['http://172.27.74.49:11434']
    elif stale == 'identity':
        app['EMBEDDING_CONFIG'] = {'provider': 'ollama-pool', 'hosts': ['http://172.27.74.49:11434']}
    else:
        app['EMBEDDING_CONCURRENCY'] = 4
    monkeypatch.setattr(chromadb, 'PersistentClient', lambda **kw: pytest.fail('Opened a stale index'))
    setup = code_with('object_index = ObjectEmbeddingIndex(').split('\nresults = []', 1)[0]
    with pytest.raises(RuntimeError, match='Stale embedding'):
        exec(compile(setup, str(NOTEBOOK), 'exec'), app)
    assert not (tmp_path / 'db').exists()


@pytest.mark.parametrize('mode', ['object', 'task', 'remaining'])
def test_force_rebuild_uses_a_fresh_directory_only_for_selected_mode(tmp_path, mode):
    (tmp_path / 'pyproject.toml').write_text('')
    source = code_with('PROJ_ROOT = resolve_project_root()')
    source = source.replace('EMBEDDING_MODE = "remaining"', f'EMBEDDING_MODE = "{mode}"')
    source = source.replace('FORCE_REBUILD = False', 'FORCE_REBUILD = True')
    directory = 'task_embeddings' if mode == 'task' else 'object_embeddings'
    index_root = tmp_path / 'db' / directory
    if mode == 'remaining':
        index_root /= 'md.json.semantics.remaining'
    old = index_root / 'qwen3-embedding-8b-172.27.74.16-11434--172.27.74.49-11434/catalog.sqlite3'
    old.parent.mkdir(parents=True)
    old.write_bytes(b'previous index')
    with patch.dict(os.environ, {'PROJ_ROOT': str(tmp_path)}, clear=True):
        app = {}
        exec(compile(source, str(NOTEBOOK), 'exec'), app)
    assert app['INDEX_DIR'].parent == old.parent.parent
    assert app['INDEX_DIR'].name.startswith('qwen3-embedding-8b-172.27.74.16-11434--172.27.74.49-11434-')
    assert app['CATALOG_PATH'] == app['INDEX_DIR'] / 'catalog.sqlite3'
    assert not app['INDEX_DIR'].exists()
    assert old.read_bytes() == b'previous index'


def test_host_environment_overrides_cannot_change_embedding_or_comparison_server(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('')
    env = dict(PROJ_ROOT=str(tmp_path), OLLAMA_EMBED_HOSTS='http://old:11434',
               OLLAMA_HOST='http://old:11434', OLLAMA_RAG_HOST='http://other:11434')
    with patch.dict(os.environ, env, clear=True):
        app = {}
        exec(compile(code_with('PROJ_ROOT = resolve_project_root()'), str(NOTEBOOK), 'exec'), app)
        exec(compile(code_with('RAG_HOST ='), str(NOTEBOOK), 'exec'), app)
    assert app['OLLAMA_HOSTS'] == app['HOSTS'] == ['http://172.27.74.16:11434', 'http://172.27.74.49:11434']
    assert app['RAG_HOST'] == 'http://172.27.74.16:11434'


@pytest.mark.parametrize('option,value', [('RIBOSOME_EMBED_REQUEST_RETRIES', '-1'),
    ('RIBOSOME_EMBED_QUARANTINE_SECONDS', 'nan'), ('RIBOSOME_EMBED_HEALTH_TIMEOUT', '0')])
def test_invalid_retry_settings_do_not_create_an_index(tmp_path, option, value):
    (tmp_path / 'pyproject.toml').write_text('')
    with patch.dict(os.environ, {'PROJ_ROOT': str(tmp_path), option: value}, clear=True):
        with pytest.raises(ValueError):
            exec(compile(code_with('PROJ_ROOT = resolve_project_root()'), str(NOTEBOOK), 'exec'), {})
    assert not (tmp_path / 'db').exists()
