"""Identity, replacement, and artifact publication regressions (offline providers)."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest

from ribosome.core.dom.embedding import (
    _canonical_json, _child_path, _embed_walk, _node_id, _semantic_payload,
    embed, embed_node, embed_walk_node,
)
from ribosome.core.dom.model import DOMClass
from test_embedding_fastcore import FakeAsyncClient, FakeCollection


class Provider:
    def __init__(self, fail_at=None, response_model='provider-model'):
        self.calls = []
        self.fail_at = fail_at
        self.response_model = response_model

    async def embed(self, model, input):
        self.calls.append(input)
        if len(self.calls) == self.fail_at:
            raise RuntimeError('provider failure')
        result = {'embeddings': [[float(len(input)), 1.0, 2.0]]}
        if self.response_model is not None:
            result['model'] = self.response_model
        return result


def run(tree, collection=None, provider=None, source='sample.md', action=None):
    collection = collection if collection is not None else FakeCollection()
    provider = provider if provider is not None else Provider()
    result = asyncio.run(embed(json.dumps(tree, ensure_ascii=False), Path(source),
        {'Section', 'Table'}, None, collection, provider, 'requested-model', action))
    return json.loads(result), collection, provider


def fixture_tree():
    return {'summary': '文档', 'blocks': [
        {'t': 'Section', 's': 'same'}, {'t': 'Section', 's': 'same'}],
        'a/~b': {'t': 'Table', 's': '表格'},
        'business': {'i': 'semantic identifier', 'm': {'meaning': 'retain'}}}


def test_duplicate_siblings_paths_identity_rerun_export_and_business_fields():
    tree = fixture_tree()
    result, collection, provider = run(tree)
    expected_root = hashlib.sha256(('sample.md' + _canonical_json(tree)).encode()).hexdigest()
    assert result['i'] == expected_root
    assert len(collection.rows) == len(provider.calls) == 4
    first, second = result['blocks']
    assert first['i'] != second['i']
    assert first['i'] == _node_id(tree['blocks'][0], expected_root, '$/blocks/0')
    assert first['m']['node_path'] == '$/blocks/0'
    assert second['m']['node_path'] == '$/blocks/1'
    assert result['a/~b']['m']['node_path'] == '$/a~1~0b'
    assert _child_path('$', 'a/~b') == '$/a~1~0b'
    assert result['business'] == tree['business']
    assert _semantic_payload(result) == tree
    original_ids = set(collection.rows)
    repeated, _, _ = run(result, collection)
    assert repeated == result
    assert set(collection.rows) == original_ids
    assert not any(op[0] == 'delete' for op in collection.operations)
    changed = copy.deepcopy(tree)
    changed['blocks'].pop()
    changed['a/~b']['s'] = 'changed'
    replacement, _, _ = run(changed, collection)
    assert set(collection.rows) == {replacement['i'], replacement['blocks'][0]['i'], replacement['a/~b']['i']}
    assert collection.operations[-1][0] == 'delete'


@pytest.mark.parametrize('source', ['sample.md', '/absolute/sample.md'])
def test_source_identity_is_exact(source):
    tree = fixture_tree()
    result, collection, _ = run(tree, source=source)
    assert result['i'] == hashlib.sha256((source + _canonical_json(tree)).encode()).hexdigest()
    assert all(row['metadatas'][0]['file_path'] == source for row in collection.records)
    other = '/absolute/sample.md' if source == 'sample.md' else 'sample.md'
    other_result, _, _ = run(tree, collection, source=other)
    assert result['i'] != other_result['i']
    assert len(collection.rows) == 8


@pytest.mark.parametrize('response_model', ['reported', None, ''])
def test_consistent_root_and_node_response_models(response_model):
    result, collection, _ = run(fixture_tree(), provider=Provider(response_model=response_model))
    expected = response_model or 'requested-model'
    assert result['m']['embed_model'] == expected
    assert all(r['metadatas'][0]['embed_model'] == expected for r in collection.records)


def test_action_once_and_completed_before_hashing_or_requests():
    tree = {'summary': 'old', 'blocks': [{'t': 'Section', 's': 'old'}]}
    seen = []
    def action(node):
        seen.append(type(node).__name__)
        if isinstance(node, dict):
            return {k: ('new' if k in ('summary', 's') else v) for k, v in node.items()}
        return node
    class CheckedProvider(Provider):
        async def embed(self, model, input):
            assert seen == ['dict', 'list', 'dict']
            return await super().embed(model, input)
    result, _, provider = run(tree, provider=CheckedProvider(), action=action)
    transformed = {'summary': 'new', 'blocks': [{'t': 'Section', 's': 'new'}]}
    assert _semantic_payload(result) == transformed
    assert result['i'] == hashlib.sha256(('sample.md'+_canonical_json(transformed)).encode()).hexdigest()
    assert provider.calls == ['new', 'new']
    assert tree['summary'] == 'old'


@pytest.mark.parametrize('tree,types,action', [([], {'Section'}, None),
    ({'summary':'x'}, {1}, None), ({'summary':'x'}, 'Section', None),
    ({'summary':'x'}, {'Section'}, lambda node: []),
    ({'summary':'x', 'blocks':[]}, {'Section'}, lambda node: {} if isinstance(node,list) else node)])
def test_invalid_input_before_requests_and_persistence(tree, types, action):
    collection, provider = FakeCollection(), Provider()
    with pytest.raises(ValueError):
        asyncio.run(embed(json.dumps(tree), Path('sample.md'), types, None, collection, provider, action=action))
    assert provider.calls == []
    assert collection.operations == []


def test_empty_summaries_have_no_generated_fields_or_rows():
    result, collection, provider = run({'summary': '   ', 'blocks':[{'t':'Section','s':''}]})
    assert not collection.rows and not provider.calls
    assert 'i' not in result and 'm' not in result
    assert 'i' not in result['blocks'][0]


@pytest.mark.parametrize('stage', ['query', 'provider', 'upsert', 'delete', 'invalid'])
def test_failures_preserve_old_rows_and_retry_converges(stage):
    collection = FakeCollection()
    tree = fixture_tree()
    run(tree, collection)
    old_ids = set(collection.rows)
    changed = copy.deepcopy(tree)
    changed['summary'] = 'replacement'
    changed['blocks'].pop()
    provider = Provider(fail_at=2 if stage == 'provider' else None)
    if stage == 'invalid':
        provider = FakeAsyncClient([{'embeddings': []}, {'embeddings': []}])
    method = {'query':'get', 'upsert':'upsert', 'delete':'delete'}.get(stage)
    original = getattr(collection, method) if method else None
    before = len(collection.operations)
    if method:
        def fail(*args, **kwargs):
            raise RuntimeError(stage + ' failure')
        setattr(collection, method, fail)
    with pytest.raises(RuntimeError, match='source sample.md'):
        run(changed, collection, provider)
    assert old_ids <= set(collection.rows)
    assert not any(op[0]=='delete' for op in collection.operations[before:])
    if stage == 'query':
        assert not provider.calls
    if stage == 'provider':
        assert len(collection.rows) == len(old_ids) + 1  # successful root upsert is recoverable
    if method:
        setattr(collection, method, original)
    result, _, _ = run(changed, collection)
    assert set(collection.rows) == {result['i'], result['blocks'][0]['i'], result['a/~b']['i']}


def test_failure_context_has_node_path_and_original_cause():
    provider = Provider(fail_at=2)
    with pytest.raises(RuntimeError) as caught:
        run(fixture_tree(), provider=provider)
    assert '$/blocks/0' in str(caught.value)
    assert isinstance(caught.value.__cause__.__cause__, RuntimeError)


def test_public_wrapper_and_private_context_route_agree_without_mutating_ancestors():
    tree = {'t':'Section','s':'parent','child':{'t':'Table','s':'child'}}
    path = ['document-id']
    collection = FakeCollection()
    result = asyncio.run(embed_walk_node(tree,path,collection,{'Section','Table'},Provider()))
    other = asyncio.run(_embed_walk(copy.deepcopy(tree),'document-id','$','',
        FakeCollection(),{'Section','Table'},Provider(),'bge-m3',set()))
    assert result == other
    assert path == ['document-id']
    assert [r['documents'][0] for r in collection.records] == ['child','parent']
    assert 'file_path' not in result['m']
    direct = asyncio.run(embed_node({'t':'Section','s':'parent','i':'stale'},
        FakeCollection(),path,{'Section'},Provider(),'bge-m3'))
    assert direct['i'] != 'stale'
    standalone = asyncio.run(embed_node({'t':'Section','s':'parent'},
        FakeCollection(),[],{'Section'},Provider(),'bge-m3'))
    assert 'document_id' not in standalone['m']


def test_business_hex_id_and_metadata_without_pipeline_schema_survive():
    value = {'t':'Business','i':'a'*64,'m':{'meaning':'keep'}}
    assert _semantic_payload(value) == value


def test_real_chroma_replacement_query_and_optional_provenance():
    client = chromadb.EphemeralClient()
    collection = client.create_collection('embedding-correctness')
    try:
        tree = fixture_tree()
        result, _, _ = run(tree, collection)
        ids = set(collection.get()['ids'])
        assert len(ids) == 4
        run(result, collection)
        assert set(collection.get()['ids']) == ids
        changed = {'summary':'changed','blocks':[{'t':'Section','s':'new'}]}
        result, _, _ = run(changed, collection)
        assert set(collection.get()['ids']) == {result['i'], result['blocks'][0]['i']}
        data = collection.get(where={'file_path':'sample.md'}, include=['metadatas'])
        assert len(data['ids']) == 2
        assert all(all(isinstance(v,(str,int,float,bool)) for v in m.values()) for m in data['metadatas'])
        queried = collection.query(query_embeddings=[[1.,1.,2.]],n_results=2)
        assert set(queried['ids'][0]) == set(data['ids'])
        standalone = asyncio.run(embed_node({'t':'Section','s':'direct'},collection,[],{'Section'},Provider(),'requested-model'))
        assert collection.get(ids=[standalone['i']])['metadatas'][0] == standalone['m']
    finally:
        client.delete_collection('embedding-correctness')


@pytest.mark.parametrize('stage', ['provider','upsert'])
@pytest.mark.parametrize('existing_artifact', [False,True])
def test_dom_failure_does_not_publish_artifact_and_retry_converges(tmp_path,stage,existing_artifact):
    source = tmp_path/'doc.md'
    source.write_text('# Title\n')
    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection('mitochondria')
    # Isolate this fixture even when Chroma reuses an ephemeral client in-process.
    previous = collection.get()['ids']
    if previous:
        collection.delete(ids=previous)
    dom = DOMClass(source,db_client=client)
    dom.semantics_json = json.dumps(fixture_tree())
    object.__setattr__(dom,'ollama_client',Provider(fail_at=2 if stage=='provider' else None))
    artifact = tmp_path/'doc_embed.json'
    if existing_artifact:
        artifact.write_bytes(b'previous successful artifact')
    class FailingCollection:
        def get(self,**kwargs):return collection.get(**kwargs)
        def upsert(self,**kwargs):raise RuntimeError('storage failure')
        def delete(self,**kwargs):return collection.delete(**kwargs)
    class Db:
        def get_or_create_collection(self,name):
            return FailingCollection() if stage=='upsert' else collection
    with patch('ribosome.core.dom.model.PersistentClient',return_value=Db()):
        with pytest.raises(RuntimeError):
            asyncio.run(dom.embed_doc(db_path=tmp_path/'db'))
    assert not dom.embed_json
    if existing_artifact:
        assert artifact.read_bytes() == b'previous successful artifact'
    else:
        assert not artifact.exists()
    class SuccessDb:
        def get_or_create_collection(self,name):return collection
    object.__setattr__(dom,'ollama_client',Provider())
    # Change the tree so the retry also has to clear any successful partial root.
    changed = fixture_tree();changed['summary']='retry';changed['blocks'].pop()
    dom.semantics_json=json.dumps(changed)
    with patch('ribosome.core.dom.model.PersistentClient',return_value=SuccessDb()):
        asyncio.run(dom.embed_doc(db_path=tmp_path/'db'))
    result=json.loads(artifact.read_text())
    assert json.loads(dom.embed_json)==result
    assert set(collection.get(where={'file_path':str(source)})['ids']) == {
        result['i'],result['blocks'][0]['i'],result['a/~b']['i']}


def test_summary_only_document_export_is_stable():
    tree = {'s':'standalone root'}
    result, collection, _ = run(tree)
    assert _semantic_payload(result) == tree
    repeated, _, _ = run(result, collection)
    assert repeated == result
    assert len(collection.rows) == 1


def test_nonfinite_vector_is_rejected_before_storage():
    provider = FakeAsyncClient([{'embeddings':[[float('nan'),1.0]]}])
    collection = FakeCollection()
    with pytest.raises(RuntimeError,match='non-finite embedding'):
        run({'summary':'root'},collection,provider)
    assert not collection.rows
