"""Application routing stays separate from shell hosts and summary clients."""
import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import nbformat
import pytest

from ribosome.ollama_config import OLLAMA_EMBED_HOST, OLLAMA_RAG_HOST


RAG_NOTEBOOKS = [Path('nbs') / name for name in [
    '05.application.rag.object.ipynb',
    '06.test.rag.object.question_with_location.ipynb',
    '06.test.rag.object.question_with_category.ipynb',
]]


@pytest.mark.parametrize('path', RAG_NOTEBOOKS)
def test_rag_and_evaluation_pin_separate_hosts_and_preserve_other_options(tmp_path, path):
    (tmp_path / 'pyproject.toml').write_text('')
    (tmp_path / '.env').write_text('OLLAMA_EMBED_HOSTS=http://stale-file:11434\nOLLAMA_RAG_HOST=http://stale-file:11434\n')
    env = dict(PROJ_ROOT=str(tmp_path), OLLAMA_HOST='http://summary:11434',
               OLLAMA_EMBED_HOSTS='http://stale-shell:11434', OLLAMA_EMBED_HOST='http://stale-shell:11434',
               OLLAMA_RAG_HOST='http://stale-shell:11434', OLLAMA_EMBED_MODEL='embed-model',
               OLLAMA_RAG_MODEL='answer-model')
    nb = nbformat.read(path, as_version=4)
    nbformat.validate(nb)
    for cell in nb.cells:
        if cell.cell_type == 'code':
            compile(cell.source, str(path), 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    with patch.dict(os.environ, env, clear=True):
        ns = {}
        exec(nb.cells[2].source, ns)
        assert ns['HOSTS'] == [OLLAMA_EMBED_HOST] == ['http://172.27.74.49:11434']
        assert ns['RAG_HOST'] == OLLAMA_RAG_HOST == 'http://172.27.74.16:11434'
        assert ns['EMBED_MODEL'] == 'embed-model' and ns['RAG_MODEL'] == 'answer-model'
        assert os.environ['OLLAMA_HOST'] == 'http://summary:11434'
        assert ns['INDEX_DIR'] == tmp_path / 'db/object_embeddings/embed-model-172.27.74.49-11434'
        assert not ns['INDEX_DIR'].exists()
        if path.name.startswith('06'):
            # The recorded config source also participates in evaluation identity.
            assignment = next(node for node in ast.parse(nb.cells[3].source).body
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                    t.id == 'EVALUATION_CONFIG_SOURCE' for t in node.targets))
            assert ast.literal_eval(assignment.value) == nb.cells[2].source
        os.environ['OBJECT_RAG_INDEX'] = 'chosen-compatible-index'
        override = {}
        exec(nb.cells[2].source, override)
        assert override['INDEX_DIR'] == tmp_path / 'chosen-compatible-index'


def test_rag_service_constructs_clients_on_the_selected_servers(tmp_path, monkeypatch):
    import chromadb
    import ollama
    nb = nbformat.read(RAG_NOTEBOOKS[0], as_version=4)
    (tmp_path / 'pyproject.toml').write_text('')
    with patch.dict(os.environ, dict(PROJ_ROOT=str(tmp_path)), clear=True):
        ns = {}
        exec(nb.cells[2].source, ns)
    directory = ns['INDEX_DIR']
    (directory / 'chroma').mkdir(parents=True)
    (directory / 'catalog.sqlite3').touch()
    clients, setups = [], []
    def client(**kwargs):
        result = SimpleNamespace(**kwargs)
        clients.append(result)
        return result
    monkeypatch.setattr(ollama, 'AsyncClient', client)
    collection = object()
    def collection_(name, **kwargs):
        assert name == 'object_summaries' and kwargs == {'embedding_function': None}
        return collection
    monkeypatch.setattr(chromadb, 'PersistentClient', lambda **kwargs:
                        SimpleNamespace(get_collection=collection_))
    def app(*args, **kwargs):
        setups.append((args, kwargs))
        return SimpleNamespace()
    ns['ObjectRAG'] = app
    exec(nb.cells[4].source, ns)
    assert [c.host for c in clients] == [OLLAMA_EMBED_HOST, OLLAMA_RAG_HOST]
    assert all(c.trust_env is False for c in clients)
    args, kwargs = setups[0]
    assert args == (directory, collection, *clients)
    assert kwargs['embedding_config'] == dict(provider='ollama-pool', hosts=[OLLAMA_EMBED_HOST])


def test_default_object_embedding_provider_uses_embedding_host(tmp_path, monkeypatch):
    import chromadb
    import ollama
    from ribosome.core.dom.embedding.object import ObjectEmbeddingIndex
    clients = []
    def client(**kwargs):
        clients.append(kwargs)
        return SimpleNamespace()
    monkeypatch.setattr(ollama, 'AsyncClient', client)
    chroma = chromadb.EphemeralClient()
    collection = chroma.create_collection(
        'deployment-default', metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    try:
        with ObjectEmbeddingIndex(tmp_path / 'catalog.sqlite3', collection=collection) as index:
            assert clients == [dict(host=OLLAMA_EMBED_HOST, trust_env=False)]
            assert index.embedding_config == dict(provider='ollama-pool', hosts=[OLLAMA_EMBED_HOST])
    finally:
        chroma.delete_collection(collection.name)


def test_legacy_dom_has_dedicated_embedding_client_and_retains_summary_client(tmp_path):
    from ribosome.core.dom.model import DOMClass
    summary = SimpleNamespace()
    dom = DOMClass.model_construct(file_path=tmp_path / 'manual.md',
        ollama_client=summary, openai_client=SimpleNamespace(), db_client=SimpleNamespace())
    try:
        assert dom.ollama_client is summary
        assert str(dom.embedding_client._client.base_url).rstrip('/') == OLLAMA_EMBED_HOST
        assert dom.embedding_client._client.trust_env is False
    finally:
        asyncio.run(dom.embedding_client.close())
