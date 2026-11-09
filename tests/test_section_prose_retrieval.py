import asyncio
import copy
import json
from pathlib import Path

import pytest

from ribosome.core.dom.embedding.object import ObjectEmbeddingIndex, collect_object_records
from ribosome.rag.object import ObjectRAG


def para(text):
    return {'t': 'Para', 'c': [{'t': 'Str', 'c': text}], 's': 'Incorrect generated paraphrase'}


def section(body):
    return {'t': 'Section', 'c': [
        {'t': 'Header', 'c': [3, ['', [], []], [{'t': 'Str', 'c': '电磁阀配置'}]]},
        {'t': 'Content', 'c': body},
    ], 'subnodes': [], 's': 'Unverified manufacturer and prerequisite claims'}


def tree():
    return {'summary': 'Manual', 'file_path': 'assets/code-docs.5.0/.md/manual.md', 'blocks': [section([
        para('输出的状态可手动强制输出，条件是上电+deadman使能。'),
        {'t': 'Para', 'c': [{'t': 'Str', 'c': '配置'}, {'t': 'Code', 'c': [['', [], []], 'OUT DO[1]']} ]},
        {'t': 'Div', 'object': {'id': 1, 'type': 'table', 'summary': 'Gripper command parameters',
                                'content': '<table>Check/NoCheck; valid range 1-8</table>'}},
    ])]}


def test_source_prose_has_traceable_context_without_generated_summary_or_table_duplication(tmp_path):
    records = collect_object_records(tree(), tmp_path/'manual_semantics.json')
    prose = [r for r in records if r.type == 'prose']
    assert len(prose) == 1
    r = prose[0]
    assert '上电+deadman使能' in r.content
    assert 'OUT DO[1]' in r.content
    assert r.section_headings == ['电磁阀配置']
    assert r.occurrence_paths == ['$/blocks/0/c/1/c/0', '$/blocks/0/c/1/c/1']
    assert r.object_path == r.occurrence_paths[0]
    assert r.parent_id == records[0].id
    assert r.id in records[0].children_ids
    assert 'Incorrect generated' not in r.summary
    assert 'manufacturer' not in r.summary
    assert '<table>' not in r.content


def test_mirrored_sections_do_not_duplicate_source_passages_and_ids_survive_summary_edits(tmp_path):
    original = tree()
    original['mirrored'] = copy.deepcopy(original['blocks'])
    before = [r for r in collect_object_records(original, tmp_path/'manual_semantics.json') if r.type == 'prose']
    assert len(before) == 1
    assert len(before[0].occurrence_paths) == 4
    original['blocks'][0]['s'] = 'Different generated summary'
    after = [r for r in collect_object_records(original, tmp_path/'manual_semantics.json') if r.type == 'prose']
    assert after[0].id == before[0].id


def test_long_sections_split_at_source_block_boundaries(tmp_path):
    t=tree();t['blocks'][0]['c'][1]['c']=[para('甲'*1500),para('乙'*1500)]
    prose=[r for r in collect_object_records(t,tmp_path/'manual_semantics.json') if r.type=='prose']
    assert [r.content for r in prose] == ['甲'*1500,'乙'*1500]
    assert prose[0].occurrence_paths == ['$/blocks/0/c/1/c/0']
    assert prose[1].occurrence_paths == ['$/blocks/0/c/1/c/1']


class Collection:
    id = 'source-prose-test'
    metadata = {'hnsw:space':'cosine'}
    configuration = {'hnsw': {'space': 'cosine'}}
    def __init__(self):
        self.metadata=dict(type(self).metadata)
        self.rows={}
    def count(self):
        return len(self.rows)
    def modify(self, *, metadata):
        self.metadata=metadata
    def upsert(self, *, ids, embeddings, documents, metadatas):
        self.rows.update({i:(v,d,m) for i,v,d,m in zip(ids,embeddings,documents,metadatas)})
    def query(self, *, query_embeddings, ids, n_results, include):
        q=query_embeddings[0]
        def distance(i):
            v=self.rows[i][0]
            return 1-sum(a*b for a,b in zip(q,v))
        chosen=sorted(ids,key=distance)[:n_results]
        return {'ids':[chosen],'distances':[[distance(i) for i in chosen]]}


class Generator:
    async def chat(self, **kwargs):
        assert '上电+deadman使能' in kwargs['messages'][-1]['content']
        payload = {name: '' for name in ('scenario','environment','expected_result','code','instruction_statement','language','manual')}
        payload.update(explanation='手动强制输出需要上电并使能Deadman。[E1]。', supported=False, citations=['E1'])
        return {'message': {'content': json.dumps(payload,ensure_ascii=False)}}


class Embedder:
    async def embed(self, *, model, input):
        return {'embeddings':[[1.0,0.0] if '电磁阀' in input else [0.0,1.0]],'model':model}


def test_configuration_prose_is_embedded_and_retrieved_as_grounded_evidence(tmp_path,monkeypatch):
    categories={'material_handling':'搬运'}
    mapping=[dict(key='manual',profile='5.0',category='material_handling',revision='test')]
    monkeypatch.setattr('ribosome.rag.object.load_mapping',lambda _: (categories,mapping))
    source=tmp_path/'assets/code-docs.5.0/.md/manual_semantics.json'
    source.parent.mkdir(parents=True);source.write_text(json.dumps(tree(),ensure_ascii=False))
    collection=Collection();embedder=Embedder();config={'provider':'fake'}
    async def run():
        index=ObjectEmbeddingIndex(tmp_path/'index/catalog.sqlite3',collection=collection,llm_client=embedder,model='fake',embedding_config=config)
        try:
            await index.index_file(source)
        finally:index.close()
        rag=ObjectRAG(tmp_path/'index',collection,embedder,mapping_dir=tmp_path,embed_model='fake',embedding_config=config,generator=Generator(),rag_model='fake')
        try:
            result=await rag.retrieve('电磁阀配置的输出前提条件是什么？','material_handling',top_k=1,profile='5.0')
            assert result['hits'][0]['record']['type']=='prose'
            assert '上电+deadman使能' in result['context']
            assert '电磁阀配置' in result['context']
            assert 'source AST $/blocks/0/c/1/c/0' in result['context']
            assert 'lines None' not in result['context']
            answer=await rag.answer('电磁阀配置的输出前提条件是什么？','material_handling',top_k=1,profile='5.0')
            assert 'Deadman' in answer['answer']
            assert 'source AST $/blocks/0/c/1/c/0' in answer['retrieved_summary']
            assert 'lines None' not in answer['retrieved_summary']
        finally:rag.close()
    asyncio.run(run())
