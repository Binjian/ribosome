"""Offline ranking checks using the application's configured category callback."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
import sqlite3
import sys

import nbformat
import pytest

from ribosome.rag.object import ObjectRAG


@pytest.fixture
def application_config(monkeypatch):
    monkeypatch.setenv('OBJECT_RAG_CATEGORY_BOOST', '0.15')
    notebook = nbformat.read('nbs/05.application.rag.object.ipynb', as_version=4)
    namespace = {}
    exec(next(c.source for c in notebook.cells if c.id == 'b4efd573'), namespace)
    return namespace


@pytest.fixture
def rag(tmp_path, monkeypatch, application_config):
    categories = dict(arc_welding='弧焊', general_operation='通用操作')
    mapping = [dict(key=key, profile=profile, category=category, revision='test')
               for key, profile, category in [
                   ('arc', '4.0', 'arc_welding'), ('arc', '5.0', 'arc_welding'),
                   ('general', '4.0', 'general_operation')]]
    monkeypatch.setattr('ribosome.rag.object.load_mapping', lambda folder: (categories, mapping))
    embedding_config = dict(provider='ollama-pool', hosts=application_config['HOSTS'])
    fingerprint = hashlib.sha256(json.dumps(
        dict(schema=1, model='fake', config=embedding_config), ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    distances = {}
    connection = sqlite3.connect(tmp_path / 'catalog.sqlite3')
    connection.executescript('''
        CREATE TABLE settings (key TEXT, value TEXT);
        CREATE TABLE active_documents (generation_id TEXT);
        CREATE TABLE objects (generation_id TEXT, record TEXT, vector_id TEXT);
    ''')
    connection.executemany('INSERT INTO settings VALUES (?, ?)', [
        ('identity', json.dumps(dict(fingerprint=fingerprint, collection_id='test'))),
        ('catalog_id', json.dumps('catalog-test')), ('dimension', json.dumps(2))])
    for generation, key, profile, distance, active in [
        ('matching', 'arc', '4.0', 0.20, True),
        ('generic', 'general', '4.0', 0.10, True),
        ('weak-match', 'arc', '5.0', 0.80, True),
        ('unmapped', 'unknown', '4.0', 0.01, True),
        ('inactive', 'arc', '4.0', 0.00, False),
    ]:
        if active:
            connection.execute('INSERT INTO active_documents VALUES (?)', (generation,))
        document = dict(id='doc', document_id='doc', type='document',
                        semantics_file=f'assets/code-docs.{profile}/.md/{key}_semantics.json')
        table = dict(id=generation, document_id='doc', type='table', parent_id=None,
                     children_ids=[], summary=f'Summary for {generation}',
                     source_file=f'{key}.md', content=f'Evidence for {generation}',
                     attributes=dict(line_start=1, line_end=3))
        connection.executemany('INSERT INTO objects VALUES (?, ?, ?)', [
            (generation, json.dumps(document), None), (generation, json.dumps(table), generation)])
        distances[generation] = distance
    connection.commit()
    connection.close()

    class Collection:
        id = 'test'
        metadata = dict(object_index_config=fingerprint, object_catalog_id='catalog-test',
                        **{'hnsw:space': 'cosine'})

        def __init__(self):
            self.calls = []

        def query(self, **kwargs):
            self.calls.append(kwargs)
            ids = sorted(kwargs['ids'], key=distances.get)[:kwargs['n_results']]
            return dict(ids=[ids], distances=[[distances[i] for i in ids]])

    class Embedder:
        def __init__(self):
            self.calls = []

        async def embed(self, **kwargs):
            self.calls.append(kwargs)
            return dict(embeddings=[[1.0, 0.0]])

    class Generator:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            return dict(message=dict(content=json.dumps(dict(
                supported=False, explanation='No documented example in this fixture.',
                scenario='', environment='', expected_result='', code='',
                instruction_statement='', language='', manual='', citations=[]))))

    collection, embedder, generator = Collection(), Embedder(), Generator()
    app = ObjectRAG(tmp_path, collection, embedder, generator, mapping_dir=tmp_path,
                    embed_model='fake', embedding_config=embedding_config, rag_model='fake',
                    rerank=application_config['rerank_by_category'])
    yield SimpleNamespace(app=app, collection=collection, embedder=embedder, generator=generator,
                          index_dir=tmp_path)
    app.close()


def test_category_bonus_changes_winner_before_top_k_and_preserves_raw_distance(rag):
    result = asyncio.run(rag.app.retrieve('question', 'arc_welding', top_k=1))
    hit, = result['hits']
    assert hit['record']['id'] == 'matching'  # Generic has the better raw cosine score.
    assert hit['distance'] == 0.20
    assert hit['similarity'] == pytest.approx(0.80)
    assert hit['category_match'] is True
    assert hit['category_boost'] == 0.15
    assert hit['score'] == pytest.approx(0.95)
    request, = rag.collection.calls
    assert set(request['ids']) == {'matching', 'generic', 'weak-match'}
    assert request['n_results'] == 3  # Overfetch before reranking, not top_k=1.
    assert 'Evidence for matching' in result['context']
    assert 'Evidence for generic' not in result['context']
    assert rag.embedder.calls == [dict(model='fake', input='question')]
    assert rag.app.connection.total_changes == 0


def test_other_categories_remain_eligible_and_can_beat_weak_matching_evidence(rag):
    result = asyncio.run(rag.app.retrieve('question', 'arc_welding', top_k=3))
    assert [h['record']['id'] for h in result['hits']] == ['matching', 'generic', 'weak-match']
    generic = result['hits'][1]
    assert generic['category_match'] is False
    assert generic['category_boost'] == 0
    assert generic['score'] == pytest.approx(0.90)


def test_zero_bonus_recovers_cosine_order_and_category_changes_preference(rag, application_config):
    result = asyncio.run(rag.app.retrieve('question', 'general_operation', top_k=2))
    assert result['hits'][0]['record']['id'] == 'generic'
    assert result['hits'][0]['category_boost'] == 0.15
    application_config['CATEGORY_SCORE_BOOST'] = 0.0
    result = asyncio.run(rag.app.retrieve('question', 'arc_welding', top_k=2))
    assert [h['record']['id'] for h in result['hits']] == ['generic', 'matching']
    assert all(h['score'] == h['similarity'] for h in result['hits'])


def test_profile_filter_still_applies_before_category_reranking(rag):
    result = asyncio.run(rag.app.retrieve('question', 'arc_welding', top_k=3, profile='5.0'))
    assert [h['record']['id'] for h in result['hits']] == ['weak-match']
    assert all(h['manual']['profile'] == '5.0' for h in result['hits'])
    assert rag.collection.calls[0]['ids'] == ['weak-match']


def test_answer_uses_configured_ranking_for_evidence_and_summary(rag):
    result = asyncio.run(rag.app.answer('question', 'arc_welding', top_k=1))
    assert result['hits'][0]['record']['id'] == 'matching'
    assert 'Summary for matching' in result['retrieved_summary']
    assert 'Summary for generic' not in result['retrieved_summary']
    request, = rag.generator.calls
    assert 'Evidence for matching' in request['messages'][1]['content']
    assert 'Evidence for generic' not in request['messages'][1]['content']
    assert len(rag.embedder.calls) == 1


def test_per_request_callback_can_override_configured_reranking(rag):
    seen = []

    def cosine_only(hits, category):
        seen.append(category)
        return hits

    result = asyncio.run(rag.app.retrieve('question', 'arc_welding', top_k=1, rerank=cosine_only))
    assert seen == ['arc_welding']
    assert result['hits'][0]['record']['id'] == 'generic'


def test_application_service_cell_and_wrappers_enable_category_reranking(rag, application_config, monkeypatch):
    clients = iter([rag.embedder, rag.generator])
    monkeypatch.setitem(sys.modules, 'ollama', SimpleNamespace(AsyncClient=lambda **kwargs: next(clients)))
    monkeypatch.setitem(sys.modules, 'chromadb', SimpleNamespace(
        PersistentClient=lambda **kwargs: SimpleNamespace(get_collection=lambda *args, **kwargs: rag.collection)))
    (rag.index_dir / 'chroma').mkdir()
    application_config.update(INDEX_DIR=rag.index_dir, EMBED_MODEL='fake', RAG_MODEL='fake')
    notebook = nbformat.read('nbs/05.application.rag.object.ipynb', as_version=4)
    exec(next(c.source for c in notebook.cells if c.id == '9c6c540c'), application_config)
    try:
        result = asyncio.run(application_config['retrieve']('question', 'arc_welding', top_k=1))
        assert result['hits'][0]['record']['id'] == 'matching'
        answer = asyncio.run(application_config['answer']('question', 'arc_welding', top_k=1))
        assert answer['hits'][0]['record']['id'] == 'matching'
    finally:
        application_config['app'].close()


def test_reranking_keeps_source_hits_unchanged_and_breaks_ties_deterministically(application_config):
    hits = [dict(distance=0.2, manual=dict(category='arc_welding'), record=dict(id=identifier))
            for identifier in ['b', 'a']]
    before = deepcopy(hits)
    ranked = application_config['rerank_by_category'](hits, 'arc_welding')
    assert [h['record']['id'] for h in ranked] == ['a', 'b']
    assert hits == before
