"""Canonical adapters and publication recovery: no providers or corpus access."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ribosome.core.document import (
    Document, DocumentGraph, EmbeddingArtifact, Relation, SourceSpan,
    adapt_layout, adapt_semantics, content_hash, export_legacy_semantics, source_revision,
)
from ribosome.core.artifacts import ArtifactRepository, CorpusManifest, PublicationConflict


def semantic_tree():
    row = {'id': 'row-1', 'type': 'table_row', 'content': 'RST-001 | Check power off.',
           'summary': 'Saved row summary', 'line_start': 9, 'line_end': 9,
           'children': [{'id': 'cell-1', 'type': 'table_cell', 'content': 'RST-001',
                         'row': 0, 'column': 0, 'rowspan': 1, 'colspan': 1}]}
    table = {'id': 'task-1', 'type': 'task', 'content': '<table>RST-001</table>',
             'summary': 'Saved task summary', 'children': [row], 'cells': [deepcopy(row['children'][0])]}
    return {'summary': 'Saved document summary', 'blocks': [
        {'t': 'Section', 'c': 'Reset', 's': 'Saved section summary', 'subnodes': [
            {'t': 'Para', 'c': 'The limit is 12 V, not 24 V.', 's': ''},
            {'t': 'Content', 'c': '<table>RST-001</table>', 's': 'Saved task summary',
             'object': table, 'subnodes': [{'object': deepcopy(row), 's': 'Saved row summary'}]},
            {'t': 'CodeBlock', 'c': 'CHECK POWER OFF\nRESET VALVE RST-002'},
        ]}]}


def graph_for(document=None, *, content=b'original source', tree=None):
    document = document or Document('logical-doc', 'Fixture', 'fixture', 'service-manual', ('old/path.md',))
    revision = source_revision(document.id, 'manual.md', content, revision_label='A')
    return adapt_semantics(tree or semantic_tree(), document, revision)


def publish(repo, graph):
    publication = repo.prepare(graph)
    return repo.commit(publication.generation)


def test_semantic_adapter_round_trip_roles_ownership_and_summary_preservation():
    source = semantic_tree()
    before = deepcopy(source)
    graph = graph_for(tree=source)
    assert source == before == export_legacy_semantics(graph)
    table = next(n for n in graph.nodes if 'task-1' in n.aliases)
    assert table.kind == 'table' and table.semantic_roles == ['task']
    assert len([n for n in graph.nodes if 'cell-1' in n.aliases]) == 1
    assert {'document', 'section', 'table', 'table_row', 'table_cell', 'paragraph', 'code'} <= {n.kind for n in graph.nodes}
    assert {s.text for s in graph.summaries} == {'Saved document summary', 'Saved section summary', 'Saved task summary', 'Saved row summary'}
    assert all(s.coverage == 'coverage_unknown' for s in graph.summaries)
    assert DocumentGraph.from_dict(json.loads(json.dumps(graph.to_dict()))).to_dict() == json.loads(json.dumps(graph.to_dict()))


def test_duplicate_content_and_typed_legacy_ids_remain_distinct():
    tree = {'objects': [{'id': 1, 'type': 'list_item', 'content': 'same'},
                        {'id': '1', 'type': 'list_item', 'content': 'same'}]}
    graph = graph_for(tree=tree)
    assert len(graph.nodes) == 3
    assert len(set(n.id for n in graph.nodes)) == 3


def test_mirror_cell_order_and_escaped_legacy_aliases():
    cells = [{'id': f'cell-{i}', 'type': 'table_cell', 'column': i, 'content': str(i)} for i in range(3)]
    row = {'id': 'row', 'type': 'table_row', 'content': 'row', 'children': [cells[2]], 'cells': deepcopy(cells)}
    graph = graph_for(tree={'objects': [row, {'id': 1, 'type': 'figure', 'content': 'one'},
                                         {'id': '{"legacy_id":1}', 'type': 'figure', 'content': 'two'}]})
    nodes = {n.id: n for n in graph.nodes}
    owner = next(n for n in graph.nodes if n.kind == 'table_row')
    assert [nodes[i].attributes['column'] for i in owner.children] == [0, 1, 2]
    assert len([n for n in graph.nodes if n.kind == 'figure']) == 2
    nested = graph_for(tree={'objects': [{'id': 'table', 'type': 'table', 'content': '', 'headers': [cells]}]})
    assert len([n for n in nested.nodes if n.kind == 'table_cell']) == 3
    with pytest.raises(ValueError, match='Self ownership'):
        graph_for(tree={'objects': [{'id': 'x', 'type': 'list', 'content': '',
                                    'children': [{'id': 'x', 'type': 'list', 'content': ''}]}]})


@pytest.mark.parametrize('damage', ['duplicate', 'parent', 'cycle', 'span', 'coordinates', 'revision', 'embedding_hash'])
def test_graph_rejects_invalid_publication_contract(damage):
    graph = graph_for()
    if damage == 'duplicate': graph.nodes.append(deepcopy(graph.nodes[-1]))
    elif damage == 'parent': graph.nodes[-1].parent_id = 'unknown'
    elif damage == 'cycle':
        root, last = graph.nodes[0], graph.nodes[-1]
        last.children.append(root.id); root.parent_id = last.id
    elif damage == 'span': graph.nodes[-1].span_ids.append('unknown')
    elif damage == 'coordinates': next(n for n in graph.nodes if n.kind == 'table_cell').attributes['rowspan'] = 0
    elif damage == 'revision': graph.summaries[0] = replace(graph.summaries[0], revision_id='wrong')
    elif damage == 'embedding_hash':
        graph.embeddings.append(EmbeddingArtifact(graph.nodes[0].id, graph.revision.id, 'source', 'exact', 'wrong', 'encoder', 'vector'))
    with pytest.raises(ValueError): graph.validate()


def test_physical_provenance_is_never_invented():
    doc = Document('layout')
    revision = source_revision(doc.id, 'manual.pdf', b'pdf')
    graph = adapt_layout([
        {'chunk_id': 'plain', 'content_type': 'prose', 'retrieval_text': 'No coordinates'},
        {'chunk_id': 'region', 'content_type': 'figure', 'retrieval_text': 'Diagram',
         'source_regions': [{'page_number': 2, 'bbox': [1, 2, 10, 20], 'region_id': 'r'}]},
    ], doc, revision)
    assert len(graph.spans) == 1
    assert graph.spans[0].page == 2 and tuple(graph.spans[0].bbox) == (1, 2, 10, 20)


def test_source_revision_ignores_parser_change_but_tracks_asset_bytes():
    a = source_revision('d', 'a.md', b'same', assets={'image': 'hash-a'}, parser_manifest={'version': 1})
    b = source_revision('d', 'moved.md', b'same', assets={'image': 'hash-a'}, parser_manifest={'version': 99})
    c = source_revision('d', 'a.md', b'same', assets={'image': 'hash-b'})
    assert a.id == b.id and c.id != a.id


def test_document_adoption_relocation_and_alias_conflicts(tmp_path):
    repo = ArtifactRepository(tmp_path)
    doc = repo.adopt_document('/old/manual.md', corpus_id='fixture')
    assert repo.adopt_document('/old/manual.md').id == doc.id
    moved = repo.relocate_document(doc.id, '/new/manual.md')
    assert moved.id == doc.id and set(moved.aliases) == {'/old/manual.md', '/new/manual.md'}
    with pytest.raises(ValueError): repo.adopt_document('/new/manual.md', document_id='other')


@pytest.mark.parametrize('boundary', ['blobs_written', 'preparation_recorded', 'prepared', 'before_pointer', 'pointer_written', 'committed'])
def test_failure_boundaries_and_restart_preserve_authoritative_publication(tmp_path, boundary):
    repo = ArtifactRepository(tmp_path)
    old_graph = graph_for()
    old = publish(repo, old_graph)
    updated = graph_for(content=b'new source')
    def fail(name):
        if name == boundary: raise RuntimeError(name)
    repo.fault_hook = fail
    with pytest.raises(RuntimeError, match=boundary):
        staged = repo.prepare(updated)
        repo.commit(staged.generation)
    restarted = ArtifactRepository(tmp_path)
    expected = updated if boundary == 'committed' else old_graph
    assert restarted.read('logical-doc').revision.id == expected.revision.id
    assert restarted.read('logical-doc', old.generation).revision.id == old_graph.revision.id
    assert not restarted.recover()['invalid']


def test_process_death_after_commit_is_recovered_without_reexecution(tmp_path):
    repo = ArtifactRepository(tmp_path)
    publish(repo, graph_for())
    code = '''
import os,sys
from ribosome.core.artifacts import ArtifactRepository
from ribosome.core.document import source_revision,adapt_semantics
repo=ArtifactRepository(sys.argv[1])
old=repo.read('logical-doc')
new=adapt_semantics(old.provenance['legacy_artifact'],old.document,source_revision(old.document.id,'manual.md',b'new process source'))
stage=repo.prepare(new)
repo.fault_hook=lambda boundary: os._exit(73) if boundary=='committed' else None
repo.commit(stage.generation)
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], timeout=20)
    assert result.returncode == 73
    restarted = ArtifactRepository(tmp_path)
    assert restarted.read('logical-doc').revision.content_sha256 == content_hash(b'new process source')
    assert len(restarted.recover()['committed']) == 2


def test_conflicting_preparations_incomplete_work_and_rollback(tmp_path):
    repo = ArtifactRepository(tmp_path)
    old = publish(repo, graph_for())
    first = repo.prepare(graph_for(content=b'first'))
    second = repo.prepare(graph_for(content=b'second'))
    repo.commit(first.generation)
    with pytest.raises(PublicationConflict): repo.commit(second.generation)
    missing = repo.prepare(graph_for(content=b'missing'), completeness={'missing': ['node']})
    with pytest.raises(ValueError, match='incomplete'): repo.commit(missing.generation)
    repo.rollback('logical-doc', old.generation)
    assert repo.read('logical-doc').revision.content_sha256 == content_hash(b'original source')


def test_snapshot_pins_and_bounded_node_payload_reads(tmp_path, monkeypatch):
    repo = ArtifactRepository(tmp_path)
    old = publish(repo, graph_for())
    with repo.snapshot() as snapshot:
        for i in range(3): publish(repo, graph_for(content=f'new {i}'.encode()))
        report = repo.cleanup(dry_run=False, grace_seconds=0)
        assert old.generation in report['protected_generations']
        assert snapshot.read('logical-doc').revision.content_sha256 == content_hash(b'original source')
        calls = []
        original = repo._read_blob
        def read(digest): calls.append(digest); return original(digest)
        monkeypatch.setattr(repo, '_read_blob', read)
        nodes = snapshot.nodes(kinds=['table_row'], limit=1)
        assert len(nodes) == len(calls) == 1
    assert old.generation in repo.cleanup(grace_seconds=0)['generations']


def test_manifest_never_tombstones_implicit_absences_and_cleanup_is_dry_by_default(tmp_path):
    repo = ArtifactRepository(tmp_path)
    publish(repo, graph_for())
    for i in range(3): publish(repo, graph_for(content=f'source {i}'.encode()))
    before = repo.stats()['publications']
    assert repo.cleanup(grace_seconds=0)['generations']
    assert repo.stats()['publications'] == before
    with pytest.raises(ValueError): repo.synchronize_manifest(CorpusManifest('fixture', (), authoritative=True))
    report = repo.synchronize_manifest(CorpusManifest('fixture', (), authoritative=True, complete=True), apply=True)
    assert report['retained_unlisted'] == ['logical-doc']
    assert repo.read('logical-doc')
    repo.synchronize_manifest(CorpusManifest('fixture', (), ('logical-doc',), True, True), apply=True)
    with repo.snapshot() as snapshot: assert snapshot.generations == {}
    assert repo.recover()['committed']
