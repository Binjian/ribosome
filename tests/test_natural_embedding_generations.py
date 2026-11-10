"""Natural-text compatibility policy uses the shared generation publisher."""
import asyncio
import json
import os

import chromadb
import pytest

os.environ.setdefault('OPENAI_API_KEY', 'offline-test')
os.environ.setdefault('DASHSCOPE_API_KEY', 'offline-test')

from ribosome.core.dom.embedding.natural_text import NaturalTextEmbeddingIndex, _semantic_payload, embed


class Provider:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def embed(self, model, input):
        self.calls.append(input)
        if self.fail:
            raise RuntimeError('provider unavailable')
        return {'model': model, 'embeddings': [[float(len(input)), 1.0]]}


def test_generation_adapter_preserves_summaries_reuses_work_and_retains_previous_on_failure(tmp_path):
    provider = Provider()
    collection = chromadb.EphemeralClient().create_collection('natural-generations',
        metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    adapter = NaturalTextEmbeddingIndex(tmp_path / 'catalog.sqlite3', collection=collection,
        llm_client=provider, model='fake')
    source = tmp_path / 'manual.md'
    source.write_text('# Manual\n\nSource content.\n')
    tree = {'summary': 'document', 'blocks': [{'t': 'Section', 's': 'saved section',
            'c': [{'t': 'Table', 's': 'saved table', 'c': []}]}]}
    def run(value):
        return json.loads(asyncio.run(embed(json.dumps(value), source, {'Section', 'Table'},
            None, adapter, provider, 'fake')))
    result = run(tree)
    assert _semantic_payload(result) == tree
    assert len(provider.calls) == 3
    generation = result['m']['generation_id']
    assert run(result) == result
    assert len(provider.calls) == 3
    provider.fail = True
    changed = {**tree, 'summary': 'replacement'}
    with pytest.raises(RuntimeError, match='provider unavailable'):
        run(changed)
    assert adapter.index._active_generation(result['m']['document_id']) == generation
    assert source.read_text() == '# Manual\n\nSource content.\n'
    provider.fail = False
    updated = run(changed)
    assert updated['m']['generation_id'] != generation
    assert updated['i'] == result['i']
    assert _semantic_payload(updated) == changed
    assert adapter.index.connection.execute('SELECT count(*) FROM generations').fetchone()[0] == 2
    adapter.close()


def test_natural_adapter_rejects_collection_model_mismatch(tmp_path):
    collection = chromadb.EphemeralClient().create_collection('natural-model-binding',
        metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    first = NaturalTextEmbeddingIndex(tmp_path / 'first.sqlite3', collection=collection,
        llm_client=Provider(), model='first')
    with pytest.raises(ValueError, match='mismatch'):
        NaturalTextEmbeddingIndex(tmp_path / 'second.sqlite3', collection=collection,
            llm_client=Provider(), model='second')
    first.close()


def test_failed_vector_publication_keeps_prior_natural_generation(tmp_path):
    provider = Provider()
    collection = chromadb.EphemeralClient().create_collection('natural-publication',
        metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    adapter = NaturalTextEmbeddingIndex(tmp_path / 'catalog.sqlite3', collection=collection,
        llm_client=provider, model='fake')
    source = tmp_path / 'source.md'
    first = json.loads(asyncio.run(adapter.embed('{"summary":"old"}', source, {'Section'})))
    class FailingCollection:
        def upsert(self, **kwargs):
            raise RuntimeError('storage unavailable')
        def __getattr__(self, name):
            return getattr(collection, name)
    adapter.index.collection = FailingCollection()
    with pytest.raises(RuntimeError, match='storage unavailable'):
        asyncio.run(adapter.embed('{"summary":"new"}', source, {'Section'}))
    assert adapter.index._active_generation(first['m']['document_id']) == first['m']['generation_id']
    adapter.index.collection = collection
    before_retry = len(provider.calls)
    asyncio.run(adapter.embed('{"summary":"new"}', source, {'Section'}))
    assert len(provider.calls) == before_retry
    adapter.close()
