"""Offline checks for the operational object-embedding notebook."""
import ast
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import chromadb
import pytest
from nbformat import read, validate

from ribosome.core.dom.embedding.object import IndexingResult, ObjectEmbeddingIndex
from test_object_embedding import Provider, record


NOTEBOOK = Path('nbs/05.application.embedding.object.ipynb')


def code_with(marker):
    return next(cell.source for cell in read(NOTEBOOK, as_version=4).cells
                if cell.cell_type == 'code' and marker in cell.source)


def helpers():
    namespace = {}
    exec(compile(code_with('def discover_semantics'), str(NOTEBOOK), 'exec'), namespace)
    return namespace


def semantics(folder, name, summary='Document'):
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / f'{name}_semantics.json'
    file.write_text(json.dumps({'summary': summary, 'objects': [record(1, 'figure', 'Object')]}))
    return file


def test_notebook_is_valid_offline_and_all_cells_compile():
    nb = read(NOTEBOOK, as_version=4)
    validate(nb)
    for cell in nb.cells:
        if cell.cell_type != 'code':
            continue
        compile(cell.source, str(NOTEBOOK), 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        assert not cell.outputs
        if 'def discover_semantics' not in cell.source:
            assert '# | notest' in cell.source


def test_configuration_reads_env_with_overrides_without_opening_clients(tmp_path, monkeypatch):
    (tmp_path / 'pyproject.toml').write_text('')
    (tmp_path / '.env').write_text('OLLAMA_HOST=http://from-file:11434\nOLLAMA_EMBED_MODEL=file-model\nOLLAMA_API_KEY=fake-secret\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    import ollama
    monkeypatch.setattr(ollama, 'AsyncClient', lambda **kw: pytest.fail('Configuration opened a provider client'))
    with patch.dict(os.environ, {'PROJ_ROOT': str(tmp_path), 'OLLAMA_EMBED_MODEL': 'override-model'}, clear=True):
        namespace = {}
        exec(compile(code_with('PROJ_ROOT = resolve_project_root()'), str(NOTEBOOK), 'exec'), namespace)
        assert namespace['EMBED_MODEL'] == 'override-model'
        assert namespace['OLLAMA_HOST'] == 'http://from-file:11434'
        assert namespace['headers'] == {'Authorization': 'Bearer fake-secret'}
        assert namespace['EMBEDDING_CONFIG'] == {'provider': 'http://from-file:11434'}
        assert namespace['INPUT_DIRS'] == [tmp_path / 'assets/code-docs.4.0/.md', tmp_path / 'assets/code-docs.5.0/.md']
        assert namespace['CATALOG_PATH'] == tmp_path / 'db/object_embeddings/catalog.sqlite3'
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


@pytest.mark.parametrize('capabilities', [['embedding'], ['completion']])
def test_preflight_checks_embedding_capability_and_closes_on_error(tmp_path, capabilities):
    namespace = helpers()
    semantics(tmp_path, 'a')
    calls = []

    class Client:
        async def show(self, model):
            calls.append(('show', model))
            return SimpleNamespace(capabilities=capabilities)

        async def close(self):
            calls.append(('close', None))

    client = Client()
    namespace.update(INPUT_DIRS=[tmp_path], AsyncClient=lambda **kw: client,
                     OLLAMA_HOST='http://fake', headers={}, REQUEST_TIMEOUT=300, EMBED_MODEL='bge-m3')
    code = compile(code_with('model_info = await ollama_client.show'), str(NOTEBOOK), 'exec',
                   flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    if 'embedding' in capabilities:
        asyncio.run(eval(code, namespace))
        assert calls == [('show', 'bge-m3')]
    else:
        with pytest.raises(ValueError, match='embedding support'):
            asyncio.run(eval(code, namespace))
        assert calls == [('show', 'bge-m3'), ('close', None)]
    assert not (tmp_path / 'db').exists()
