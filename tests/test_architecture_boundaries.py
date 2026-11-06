"""Offline regressions for source contracts, conditioning, and publication."""
import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import chromadb
import pytest

os.environ.setdefault('OPENAI_API_KEY', 'offline-test')
os.environ.setdefault('DASHSCOPE_API_KEY', 'offline-test')

from layout_bundle_factory import build_synthetic_bundle
from ribosome.core.dom import model as dom_model
from ribosome.preprocessing.conditioning import markdown as conditioning
from ribosome.preprocessing.ocr.layout_bundle import LayoutIngestionResult, ingest_layout_bundle
from ribosome.retrieval.layout_rag import ChromaDenseIndex, HybridIndexer, HybridRetriever, SQLiteRetrievalRecordStore


def dom_fixture(tmp_path):
    path = tmp_path / 'manual.md'
    path.write_text('# Original\n')
    dom = dom_model.DOMClass.model_construct(file_path=path, root_path=tmp_path,
        ollama_client=object(), openai_client=object(), db_client=object(), title='manual')
    dom._sync_artifact_fields()
    return dom


def test_changed_source_does_not_load_old_dom_artifacts(tmp_path, monkeypatch):
    dom = dom_fixture(tmp_path)
    dom._publish_artifact('ast', '{"blocks": ["old AST"]}')
    dom._publish_artifact('semantics', '{"summary": "preserved old summary"}')
    saved = dom.semantics_json_file.read_bytes()
    dom.file_path.write_text('# Changed\n')
    monkeypatch.setattr(dom_model.pypandoc, 'convert_text', lambda *a, **kw: '{"blocks": ["new AST"]}')
    monkeypatch.setattr(dom_model, 'reorg_node', lambda node, **kw: node)
    dom.setup()
    assert json.loads(dom.ast_json)['blocks'] == ['new AST']
    assert dom.semantics_json is None
    assert dom.semantics_json_file.read_bytes() == saved


def test_unverified_legacy_dom_summary_is_not_implicitly_overwritten(tmp_path, monkeypatch):
    dom = dom_fixture(tmp_path)
    dom.semantics_json_file.write_text('{"summary":"historic"}')
    before = dom.semantics_json_file.read_bytes()
    monkeypatch.setattr(dom_model.pypandoc, 'convert_text', lambda *a, **kw: '{"blocks": []}')
    monkeypatch.setattr(dom_model, 'reorg_node', lambda node, **kw: node)
    dom.setup()
    assert dom.artifact_compatibility
    with pytest.raises(ValueError, match='unknown source provenance'):
        asyncio.run(dom.textualize())
    assert dom.semantics_json_file.read_bytes() == before


def test_dom_publication_journal_recovers_manifest_before_compatibility(tmp_path, monkeypatch):
    dom = dom_fixture(tmp_path)
    original = dom_model.DOMClass._atomic_artifact
    def fail_manifest(path, text):
        if path.name.endswith('_artifacts.manifest.json'):
            raise OSError('injected projection failure')
        return original(path, text)
    monkeypatch.setattr(dom_model.DOMClass, '_atomic_artifact', staticmethod(fail_manifest))
    dom._publish_artifact('semantics', '{"summary":"complete"}')
    assert dom.semantics_json_file.read_text() == '{"summary":"complete"}'
    assert dom._artifact_path('_artifacts.pending.json').exists()
    monkeypatch.setattr(dom_model.DOMClass, '_atomic_artifact', staticmethod(original))
    assert dom._artifact_compatible('semantics', dom.semantics_json_file)
    assert not dom._artifact_path('_artifacts.pending.json').exists()


def test_reorg_forwards_supplied_action(tmp_path, monkeypatch):
    dom = dom_fixture(tmp_path)
    dom.raw_json = '{"blocks": []}'
    monkeypatch.setattr(dom_model, 'reorg_node', lambda node, section_level, action: action(node))
    assert json.loads(dom.reorg(lambda value: {**value, 'applied': True}))['applied']


def test_legacy_runner_keeps_published_semantics(tmp_path):
    path = tmp_path / 'manual.md'
    path.write_text('source')
    output = tmp_path / 'manual_semantics.json'
    class FakeDOM:
        def __init__(self, file, **kwargs):
            self.ast_json = '{"blocks": [{"t":"Para"}]}'
            self.semantics_json = None
        def setup(self):
            pass
        async def textualize(self):
            self.semantics_json = '{"blocks": [], "summary":"published"}'
            output.write_text(self.semantics_json)
    nb = json.loads(Path('nbs/02.process.semantics.ipynb').read_text())
    namespace = dict(Path=Path, DOMClass=FakeDOM, ollama_client=None, openai_client=None, json=json)
    exec(''.join(nb['cells'][16]['source']), namespace)
    asyncio.run(namespace['process_one'](path))
    assert json.loads(output.read_text())['summary'] == 'published'


def region(content, page=1):
    return SimpleNamespace(content=content, payload=content, page_number=page, region_index=1, asset=None)


def test_full_table_renderer_emits_recognized_header_exactly_once():
    first = '<table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table>'
    second = first.replace('<td>A</td>', '<td>B</td>')
    rendered = '\n'.join(conditioning._render_table_component(
        [region(first), region(second, 2)], 'table', conditioning.MarkdownConditioningConfig()))
    assert rendered.count('| Name | Value |') == 1
    assert rendered.count('| A | 1 |') == 1
    assert rendered.count('| B | 1 |') == 1


@pytest.mark.parametrize('table', [
    '<table><tr><td>A</td><td>1</td></tr><tr><td>B</td><td>2</td></tr></table>',
    '<table><tr><th>Name</th><th>Value</th></tr><tr><td rowspan="2">A</td><td>1</td></tr><tr><td>2</td></tr></table>',
])
def test_headerless_and_merged_tables_preserve_original_structure(table):
    rendered = '\n'.join(conditioning._render_table_component(
        [region(table)], 'table', conditioning.MarkdownConditioningConfig()))
    assert table.replace('<table>', '') in rendered
    assert 'data-source-header=' in rendered
    assert conditioning.validate_semantics_ready_markdown(rendered).eligible


def layout_fixture(tmp_path):
    paths = build_synthetic_bundle(tmp_path / 'bundle', with_pdf=False)
    result = ingest_layout_bundle(paths['layout'], reconcile_native_text=False)
    store = SQLiteRetrievalRecordStore(tmp_path / 'catalog.sqlite3')
    dense = ChromaDenseIndex(chromadb.PersistentClient(path=str(tmp_path / 'vectors')), embedding_model='publication-test')
    return result, store, dense


def index(indexer, result, vector=(1.0, 0.0)):
    parents = [parent for parent in result.sections if parent.summary and not parent.is_toc]
    return asyncio.run(indexer.index(result,
        child_embeddings=[list(vector)] * len(result.indexable_records),
        parent_embeddings=[list(vector)] * len(parents)))


def test_layout_catalog_commit_failure_preserves_prior_publication(tmp_path, monkeypatch):
    result, store, dense = layout_fixture(tmp_path)
    indexer = HybridIndexer(store, dense)
    index(indexer, result)
    before = store.publication_info()
    old_ids = set(dense.children.get(include=[])['ids'])
    changed = LayoutIngestionResult(result.document, result.sections, tuple(
        replace(record, exact_text='NEW ' + record.exact_text) for record in result.records))
    def fail(*args):
        raise sqlite3.OperationalError('injected catalog commit failure')
    monkeypatch.setattr(store, 'publish_generation', fail)
    with pytest.raises(sqlite3.OperationalError, match='injected'):
        index(indexer, changed, (0.0, 1.0))
    assert store.publication_info() == before
    assert set(dense.children.get(include=[])['ids']) == old_ids
    assert all(not hit.record.exact_text.startswith('NEW ') for hit in
               HybridRetriever(store, dense_index=dense).retrieve('MOVS', query_embedding=[0.0, 1.0]))
    store.close()


def test_prepared_dense_generation_is_invisible_after_restart(tmp_path):
    result, store, dense = layout_fixture(tmp_path)
    index(HybridIndexer(store, dense), result)
    before = store.publication_info()
    changed = LayoutIngestionResult(result.document, result.sections, tuple(
        replace(record, exact_text='NEW ' + record.exact_text) for record in result.records))
    generation = store.prepare_generation(changed, embedding_model=dense.embedding_model)
    parents = [parent for parent in changed.sections if parent.summary and not parent.is_toc]
    dense.stage_generation(generation, changed,
        child_embeddings=[[0.0, 1.0]] * len(changed.indexable_records),
        parent_embeddings=[[0.0, 1.0]] * len(parents))
    # Simulate process death after durable vector staging: no exception cleanup.
    store.close()
    store = SQLiteRetrievalRecordStore(tmp_path / 'catalog.sqlite3')
    assert store.publication_info() == before
    assert any(row['generation_id'] == generation for row in store.prepared_generations())
    retriever = HybridRetriever(store, dense_index=dense)
    evidence = retriever.retrieve_evidence('MOVS', query_embedding=[0.0, 1.0])
    assert evidence and all('NEW ' not in item.exact_text for item in evidence)
    with retriever._publication_snapshot():
        where = dense._query_where()
        assert generation not in json.dumps(where)
    store.close()


def test_reader_snapshot_keeps_old_catalog_and_dense_generation_during_publish(tmp_path):
    result, reader, dense = layout_fixture(tmp_path)
    index(HybridIndexer(reader, dense), result)
    writer = SQLiteRetrievalRecordStore(tmp_path / 'catalog.sqlite3')
    retriever = HybridRetriever(reader, dense_index=dense)
    changed = LayoutIngestionResult(result.document, result.sections, tuple(
        replace(record, exact_text='NEW ' + record.exact_text) for record in result.records))
    with retriever._publication_snapshot():
        old_publication = reader.publication_info()
        index(HybridIndexer(writer, dense), changed, (0.0, 1.0))
        assert reader.publication_info() == old_publication
        hits = retriever._retrieve('MOVS', query_embedding=[0.0, 1.0])
        assert hits and all(not hit.record.exact_text.startswith('NEW ') for hit in hits)
        assert all('NEW ' not in retriever.expander.expand(hit).exact_text for hit in hits)
    assert any(hit.record.exact_text.startswith('NEW ') for hit in retriever.retrieve('MOVS'))
    reader.close()
    writer.close()
