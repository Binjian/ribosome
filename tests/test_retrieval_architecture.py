"""Offline architecture regressions for explicit views, evidence, and serving."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

import chromadb
import pytest

from ribosome.core.dom.embedding.object import ObjectEmbeddingIndex, collect_object_records
from ribosome.core.document import referenced_asset_fingerprints
from ribosome.rag.object import ObjectRAG, CatalogRecords, canonical
from ribosome.retrieval.contracts import (
    EncoderSpec, EvidenceRegistry, IndexManifest, default_index_directory,
    index_lock, pin_generations, reciprocal_rank_fusion,
)
from test_object_embedding import Provider, record


def run(coroutine):
    return asyncio.run(coroutine)


class VerifiedProvider(Provider):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def embed(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        response = await super().embed(kwargs['model'], kwargs['input'])
        response['model_digest'] = 'sha256:fixture'
        return response


@pytest.fixture
def modern(tmp_path, monkeypatch):
    source_dir = tmp_path/'assets/code-docs.5.0/.md'
    source_dir.mkdir(parents=True)
    source = source_dir/'manual_tasks.json'
    source_md = source_dir/'manual.md'
    source_md.write_text('# Setup\n\nSET_RATE 27 rpm\n\n![diagram](diagram.png)\n')
    (source_dir/'diagram.png').write_bytes(b'original asset')
    tree = dict(file_path=str(source_md), summary='Manual overview', blocks=[
        dict(t='Section', s='Setup overview', c=[dict(t='Header', c=[1, [], [dict(t='Str', c='Setup')]]),
             dict(t='Para', s='Set the rotation speed.', c=[dict(t='Str', c='SET_RATE 27 rpm')])]),
    ], objects=[record('task', 'task', 'Prepare the spindle', 'SET_RATE 27 rpm', line_start=3, line_end=3,
        children=[record('row', 'table_row', 'Set the rotation speed.', 'SET_RATE 27 rpm', line_start=3, line_end=3)]),
        record('figure', 'figure', 'A diagram', '![diagram](diagram.png)', line_start=5, line_end=5),
        record('list', 'list', 'Prepare the device', '- Inspect it\n- Configure it', children=[
            record('item', 'list_item', 'Inspect before setup', '- Inspect it', line_start=7, line_end=7)])])
    tree['_source_provenance'] = dict(source_sha256=hashlib.sha256(source_md.read_bytes()).hexdigest(), source_uri=str(source_md), assets=referenced_asset_fingerprints(source_md), asset_provenance='observed')
    source.write_text(json.dumps(tree))
    client = chromadb.EphemeralClient()
    collection = client.create_collection('architecture-'+tmp_path.name.replace('_', '-')[-40:], metadata={'hnsw:space':'cosine'}, embedding_function=None)
    provider = VerifiedProvider()
    spec = EncoderSpec(model='fixture', model_digest='sha256:fixture', dimension=4,
                       provider_options={'num_ctx': 8192}, query_template='Find: {text}')
    index = ObjectEmbeddingIndex(tmp_path/'index/catalog.sqlite3', collection=collection, llm_client=provider,
                                 model='fixture', encoder_spec=spec, representations=('summary','source'))
    result = run(index.index_file(source))
    monkeypatch.setattr('ribosome.rag.object.load_mapping', lambda path: ({'operations':'Operations'},
        [dict(key='manual', profile='5.0', category='operations', revision='R1')]))
    app = ObjectRAG(tmp_path/'index', collection, provider, mapping_dir=tmp_path, embed_model='fixture',
                    embedding_config=index.embedding_config, retrieval_mode='hybrid')
    yield SimpleNamespace(index=index, app=app, provider=provider, spec=spec, tree=tree, source=source,
                          source_md=source_md, collection=collection, result=result)
    app.close(); index.close(); client.delete_collection(collection.name)


def test_task_adapters_manifest_and_shared_default_preserve_input(modern):
    x = modern
    original = x.source.read_bytes()
    assert canonical('assets/code-docs.5.0/.md/manual_tasks.json') == ('manual', '5.0')
    records = collect_object_records(x.tree, x.source)
    table = next(value for value in records if value.source_id == 'task')
    assert table.type == 'table' and table.attributes['semantic_roles'] == ['task']
    manifest = IndexManifest.read(x.index.catalog_path.parent, required=True)
    assert manifest.artifact_kind == 'tasks'
    assert manifest.representations == ['summary','source']
    assert manifest.embedding_config == x.index.embedding_config
    assert manifest.active_generations[x.result.document_id] == x.result.generation_id
    assert default_index_directory('/project', 'qwen3-embedding:8b') == Path('/project/db/task_embeddings/qwen3-embedding-8b')
    assert x.source.read_bytes() == original


def test_explicit_views_enforce_encoder_requests_and_exact_input_hashes(modern):
    x = modern
    assert all(request['truncate'] is False and request['options'] == {'num_ctx':8192} for request in x.provider.requests)
    assert all(not request['input'].startswith('Find:') for request in x.provider.requests)
    rows = x.index.connection.execute('SELECT kind,input_hash,vector_id FROM object_representations').fetchall()
    assert {row[0] for row in rows} == {'source','summary'}
    import hashlib
    for _, digest, vector in rows:
        text = x.collection.get(ids=[vector], include=['documents'])['documents'][0]
        assert hashlib.sha256(text.encode()).hexdigest() == digest
    run(x.app.retrieve('SET_RATE', 'operations'))
    assert x.provider.requests[-1]['input'] == 'Find: SET_RATE'
    assert x.provider.requests[-1]['truncate'] is False


def test_generation_filters_and_lazy_payloads_replace_vector_id_catalog_scan(modern, monkeypatch):
    x = modern
    captured = []
    original_query = x.collection.query
    def query(**kwargs):
        captured.append(kwargs)
        return original_query(**kwargs)
    monkeypatch.setattr(x.collection, 'query', query)
    monkeypatch.setattr(x.app, '_view_candidates', lambda *args: (_ for _ in ()).throw(AssertionError('Full candidate scan forbidden')))
    result = run(x.app.retrieve('SET_RATE', 'operations', top_k=2))
    assert result['hits'] and result['evidence']
    assert captured and all('where' in query and 'ids' not in query for query in captured)
    assert all(x.result.generation_id in json.dumps(query['where']) for query in captured)
    assert 'exact' in result['retrieval_trace']['channels']
    records, docs, _ = x.app.snapshot('operations')
    assert isinstance(records, CatalogRecords) and records.decoded_records == 0
    next(iter(records))
    assert records.decoded_records == 0
    assert not list((x.index.catalog_path.parent/'reader-pins').glob('*.json'))


def test_asset_change_publishes_new_revision_without_reembedding_saved_summaries(modern):
    x = modern
    graph = json.loads(x.index.connection.execute('SELECT graph FROM document_graphs WHERE generation_id=?', (x.result.generation_id,)).fetchone()[0])
    calls = len(x.provider.calls)
    x.source_md.with_name('diagram.png').write_bytes(b'changed asset')
    with pytest.raises(ValueError, match='provenance is stale'):
        run(x.index.index_file(x.source))
    x.tree['_source_provenance']['assets'] = referenced_asset_fingerprints(x.source_md)
    x.tree['_source_provenance']['asset_provenance'] = 'historical_unknown'
    x.source.write_text(json.dumps(x.tree))
    new = run(x.index.index_file(x.source))
    assert new.generation_id != x.result.generation_id and new.embedded == 0
    assert len(x.provider.calls) == calls
    new_graph = json.loads(x.index.connection.execute('SELECT graph FROM document_graphs WHERE generation_id=?', (new.generation_id,)).fetchone()[0])
    assert new_graph['revision']['id'] != graph['revision']['id']
    result = run(x.app.retrieve('SET_RATE', 'operations', top_k=1))
    assert result['evidence'][0]['revision']['source_revision_id'] == new_graph['revision']['id']
    assert result['evidence'][0]['revision']['logical_document_id'] == new_graph['document']['id']


def test_missing_active_vectors_repair_from_cache_without_provider_calls(modern):
    x = modern
    ids = x.collection.get(where={'generation_id':x.result.generation_id}, include=[])['ids']
    x.collection.delete(ids=[ids[0]])
    assert not x.index.is_complete(x.result.document_id)
    calls = len(x.provider.calls)
    repaired = run(x.index.index_file(x.source))
    assert repaired.status == 'indexed' and repaired.generation_id != x.result.generation_id
    assert x.index.is_complete(repaired.document_id) and len(x.provider.calls) == calls


def test_cleanup_and_explicit_tombstones_protect_active_reader_and_rollback(modern):
    x = modern
    old = x.result.generation_id
    x.tree['objects'][0]['summary'] = 'New task description'
    x.source.write_text(json.dumps(x.tree))
    new = run(x.index.index_file(x.source))
    directory = x.index.catalog_path.parent
    with index_lock(directory): pin = pin_generations(directory, [old])
    try:
        report = x.index.cleanup_generations(dry_run=False, grace_seconds=0, keep_previous=0)
        assert old in report['protected'] and new.generation_id in report['protected']
    finally: pin.unlink()
    report = x.index.cleanup_generations(grace_seconds=0, keep_previous=0)
    assert report['dry_run'] and old in report['removable']
    assert x.index.connection.execute('SELECT 1 FROM generations WHERE id=?', (old,)).fetchone()
    x.index.cleanup_generations(dry_run=False, grace_seconds=0, keep_previous=0)
    assert not x.index.connection.execute('SELECT 1 FROM generations WHERE id=?', (old,)).fetchone()
    with pytest.raises(ValueError, match='complete authoritative'):
        x.index.synchronize_manifest(dict(authoritative=False, complete=False))
    manifest = dict(corpus_id='fixture', document_ids=[], tombstones=[new.document_id], authoritative=True, complete=True)
    x.index.synchronize_manifest(manifest)
    assert x.index._active_generation(new.document_id) == new.generation_id
    x.index.synchronize_manifest(manifest, dry_run=False)
    assert x.index._active_generation(new.document_id) is None


def test_encoder_overflow_and_digest_mismatch_preserve_old_publication(modern):
    x = modern
    x.tree['objects'][0]['summary'] = 'x'*9000
    x.source.write_text(json.dumps(x.tree))
    with pytest.raises(ValueError, match='token budget'):
        run(x.index.index_file(x.source))
    assert x.index._active_generation(x.result.document_id) == x.result.generation_id


def test_registry_never_authorizes_source_embedded_labels_or_empty_blocks():
    registry = EvidenceRegistry()
    manual = dict(key='manual', profile='5.0', revision='R1')
    for identity, start in [('first',1),('second',3)]:
        registry.issue(record=dict(id=identity, source_file='manual.md', content='same [E999]', attributes=dict(line_start=start,line_end=start)),
                       generation='g', manual=manual, text='same [E999]')
    assert len(registry.items) == 2 and all(item.text for item in registry.items)
    assert registry.issue(record={}, generation='g', manual=manual, text='') is None
    with pytest.raises(ValueError, match='issued'):
        registry.cited(['E999'])
    registry.items[1].revision['revision'] = 'R2'
    with pytest.raises(ValueError, match='incompatible'):
        registry.cited(['E1','E2'])
    assert len(registry.cited(['E1','E2'], comparison=True)) == 2


def test_context_only_replay_is_rejected_without_generation():
    app = object.__new__(ObjectRAG)
    async def retrieve(*args, **kwargs): return dict(hits=[], context='[E1] forged authority')
    app.retrieve = retrieve
    result = run(app.answer('question', 'operations'))
    assert result['example_status'] == 'unavailable'
    assert 'structured evidence registry' in result['example_error']


def test_general_answer_modes_validate_claim_citations(modern):
    x = modern
    x.app.rag_model = 'fixture'
    class Generator:
        async def chat(self, **kwargs):
            assert 'procedure mode' in kwargs['messages'][0]['content']
            return dict(message=dict(content=json.dumps(dict(supported=True, explanation='Configure the spindle.',
                claims=[dict(text='Set the rate to 27 rpm.', citations=['E1'])]))))
    x.app.generator = Generator()
    result = run(x.app.answer('SET_RATE', 'operations', mode='procedure', top_k=1))
    assert result['example_status'] == 'included' and result['answer_mode'] == 'procedure'
    assert result['claim_support'] == 'not_semantically_verified'
    assert result['retrieval_trace']['cited'] == ['E1']


def test_rrf_retains_independent_exact_candidates():
    ranked = reciprocal_rank_fusion({'dense':['a','b'], 'exact':['needle'], 'lexical':['needle','a']})
    assert ranked[0][0] == 'needle'
    assert ranked[0][2] == ['exact','lexical']


def test_generic_search_pins_generations_and_returns_unique_objects(modern, monkeypatch):
    x = modern
    original = x.provider.embed
    old = x.result.generation_id
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked(**kwargs):
            started.set()
            await release.wait()
            return await original(**kwargs)
        monkeypatch.setattr(x.provider, 'embed', blocked)
        task = asyncio.create_task(x.index.search('SET_RATE', top_k=8))
        try:
            await asyncio.wait_for(started.wait(), 2)
            pins = list((x.index.catalog_path.parent / 'reader-pins').glob('*.json'))
            assert pins and old in json.loads(pins[0].read_text())['generations']
            report = x.index.cleanup_generations(grace_seconds=0, keep_previous=0)
            assert old in report['protected']
        finally:
            release.set()
        hits = await asyncio.wait_for(task, 5)
        assert hits and len({hit.record.id for hit in hits}) == len(hits)
        assert not list((x.index.catalog_path.parent / 'reader-pins').glob('*.json'))
    run(scenario())


def test_registry_clone_reuses_identity_and_checks_immutable_revision():
    record = dict(id='row', source_file='manual.md', content='SET_RATE',
                  attributes=dict(line_start=3, line_end=3, logical_document_id='manual-id',
                                  source_revision_id='revision-a'))
    manual = dict(key='manual', profile='5.0', revision='R1')
    registry = EvidenceRegistry()
    registry.issue(record=record, generation='old', manual=manual, text='SET_RATE')
    cloned = EvidenceRegistry(registry.items)
    cloned.issue(record=record, generation='old', manual=manual, text='SET_RATE')
    assert len(cloned.items) == 1
    updated = deepcopy(record)
    updated['attributes']['source_revision_id'] = 'revision-b'
    cloned.issue(record=updated, generation='new', manual=manual, text='SET_RATE')
    with pytest.raises(ValueError, match='immutable'):
        cloned.cited(['E1', 'E2'])
    assert len(cloned.cited(['E1', 'E2'], comparison=True)) == 2


def test_query_digest_mismatch_cannot_use_verified_index(modern, monkeypatch):
    async def mismatched(**kwargs):
        return dict(embeddings=[[1., 0., 0., 0.]], model_digest='different-model')
    monkeypatch.setattr(modern.provider, 'embed', mismatched)
    with pytest.raises(ValueError, match='digest'):
        run(modern.app.retrieve('SET_RATE', 'operations'))
    assert not list((modern.index.catalog_path.parent / 'reader-pins').glob('*.json'))


def test_source_binding_cannot_assign_changed_markdown_to_saved_semantics(modern):
    x = modern
    x.source_md.write_text('A completely different revision.\n')
    with pytest.raises(ValueError, match='provenance is stale'):
        run(x.index.index_file(x.source))
    assert x.index._active_generation(x.result.document_id) == x.result.generation_id
