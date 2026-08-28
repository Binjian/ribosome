import asyncio
import json
import sqlite3
import tomllib
from dataclasses import replace
from pathlib import Path

import chromadb
import pytest
from layout_bundle_factory import build_synthetic_bundle
from ribosome.preprocessing.ocr.layout_bundle import (
    LayoutIngestionResult,
    collect_region_visual_assets,
    ingest_layout_bundle,
)
from ribosome.retrieval.layout_rag import (
    ChromaDenseIndex,
    CitationAssembler,
    HybridIndexer,
    HybridRetriever,
    LayoutRAGConfig,
    RetrievalEvalCase,
    SearchHit,
    SQLiteRetrievalRecordStore,
    evaluate_retriever,
)


def _result(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle", with_pdf=False)
    return ingest_layout_bundle(bundle["layout"], reconcile_native_text=False)


def test_sqlite_replace_is_idempotent_and_removes_stale_chunks(tmp_path):
    result = _result(tmp_path)
    store = SQLiteRetrievalRecordStore(tmp_path / "layout-rag.sqlite3")
    store.replace(result)
    first_counts = store.counts(result.document.document_id)
    store.replace(result)
    assert store.counts(result.document.document_id) == first_counts

    removed = result.records[-1]
    reduced = LayoutIngestionResult(result.document, result.sections, result.records[:-1])
    store.replace(reduced)
    assert store.counts(result.document.document_id)["chunks"] == first_counts["chunks"] - 1
    assert store.get_record(removed.chunk_id) is None
    store.close()


def test_exact_and_chinese_lexical_queries_preserve_source_tokens(tmp_path):
    result = _result(tmp_path)
    with SQLiteRetrievalRecordStore() as store:
        store.replace(result)
        retriever = HybridRetriever(store)
        for query in ("MOVS", "OUT_T", "IO", "运动指令", "P<参数 1>"):
            hits = retriever.retrieve(query)
            assert hits, query
        exact = retriever.retrieve("MOVS P<参数 1> OUT_T IO")[0].record
        assert "MOVS P<参数 1> OUT_T IO" in exact.exact_text
        assert exact.instruction_code == "MOVS"


def test_evidence_expansion_stays_in_parent_and_emits_bbox_citation(tmp_path):
    result = _result(tmp_path)
    with SQLiteRetrievalRecordStore() as store:
        store.replace(result)
        evidence = HybridRetriever(store).retrieve_evidence("MOVS OUT_T", limit=1)[0]
        assert all(record.parent_id == evidence.hit.record.parent_id for record in evidence.records)
        assert evidence.citation.startswith("[SX000001 A/1, ")
        payload = CitationAssembler.citation_payload(evidence)
        assert payload["region_ids"]
        assert payload["bboxes"]
        assert payload["exact_text"]
        assert len(payload["evidence"]) == len(set(payload["region_ids"]))
        assert payload["source_markdown_uri"].endswith(".md")
        assert payload["source_layout_uri"].endswith(".layout.json")
        assert payload["source_hashes"]["markdown"]
        assert all("exact_text" in item and "quality_flags" in item and "asset_path" in item for item in payload["evidence"])


def test_chroma_uses_explicit_vectors_and_hybrid_fusion(tmp_path):
    result = _result(tmp_path)
    store = SQLiteRetrievalRecordStore()
    dense = ChromaDenseIndex(chromadb.EphemeralClient(), embedding_model="fake-model")

    def embed(texts):
        return [[float("MOVS" in text), float("JUMP" in text), 1.0] for text in texts]

    asyncio.run(HybridIndexer(store, dense).index(result, embedder=embed))
    hits = HybridRetriever(store, dense_index=dense).retrieve(
        "joint motion",
        query_embedding=[1.0, 0.0, 1.0],
    )
    assert hits
    assert hits[0].record.instruction_code == "MOVS"
    store.close()


def test_dense_runtime_failure_rolls_back_both_indexes(tmp_path):
    result = _result(tmp_path)
    store = SQLiteRetrievalRecordStore()
    dense = ChromaDenseIndex(chromadb.EphemeralClient(), embedding_model="rollback-model")
    indexer = HybridIndexer(store, dense)
    parents = [parent for parent in result.sections if parent.summary and not parent.is_toc]
    asyncio.run(
        indexer.index(
            result,
            child_embeddings=[[1.0, 0.0, 0.0]] * len(result.indexable_records),
            parent_embeddings=[[1.0, 0.0, 0.0]] * len(parents),
        )
    )
    before_counts = store.counts(result.document.document_id)
    before_dense_ids = set(dense.children.get(include=[])["ids"])
    reduced = LayoutIngestionResult(result.document, result.sections, result.records[:-1])

    with pytest.raises(Exception, match="dimension"):
        asyncio.run(
            indexer.index(
                reduced,
                child_embeddings=[[1.0, 0.0]] * len(reduced.indexable_records),
                parent_embeddings=[[1.0, 0.0]] * len(parents),
            )
        )

    assert store.counts(result.document.document_id) == before_counts
    assert set(dense.children.get(include=[])["ids"]) == before_dense_ids
    store.close()


def test_chroma_namespaces_hash_raw_version_and_embedding_model():
    client = chromadb.EphemeralClient()
    first = ChromaDenseIndex(client, pipeline_version="x/y", embedding_model="model-a")
    colliding_sanitized = ChromaDenseIndex(client, pipeline_version="x?y", embedding_model="model-a")
    different_model = ChromaDenseIndex(client, pipeline_version="x/y", embedding_model="model-b")

    assert len({first.children.name, colliding_sanitized.children.name, different_model.children.name}) == 3
    with pytest.raises(ValueError, match="embedding_model"):
        ChromaDenseIndex(client)


def test_legacy_parent_fts_schema_is_rebuilt(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE VIRTUAL TABLE parents_fts USING fts5(parent_id,summary,section_path,instruction_code)"
    )
    connection.close()

    with SQLiteRetrievalRecordStore(path) as store:
        columns = {row[1] for row in store.connection.execute("PRAGMA table_info(parents_fts)")}
        assert "pipeline_version" in columns
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] >= 2
        result = _result(tmp_path / "bundle")
        store.replace(result)
        assert store.search_parents("MOVS")


def test_retrieval_metrics_and_subpackage_packaging(tmp_path):
    result = _result(tmp_path)
    with SQLiteRetrievalRecordStore() as store:
        store.replace(result)
        retriever = HybridRetriever(store)
        relevant = retriever.retrieve("MOVS", limit=1)[0].record
        metrics = evaluate_retriever(
            retriever,
            [
                RetrievalEvalCase(
                    query="MOVS",
                    relevant_chunk_ids=(relevant.chunk_id,),
                    relevant_parent_ids=(relevant.parent_id,),
                    relevant_pages=(relevant.page_start,),
                )
            ],
            k=3,
        )
        assert metrics.child_recall_at_k == 1.0
        assert metrics.parent_recall_at_k == 1.0
        assert metrics.page_recall_at_k == 1.0
        assert metrics.mean_reciprocal_rank == 1.0

        unanswerable = evaluate_retriever(
            retriever,
            [RetrievalEvalCase(query="", expected_answerable=False)],
            k=3,
        )
        assert unanswerable.unanswerable_cases == 1
        assert unanswerable.abstention_accuracy == 1.0

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["setuptools"]["packages"]["find"]["include"] == ["ribosome*"]


def test_pipeline_versions_coexist_and_queries_are_scoped(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle", with_pdf=False)
    first = ingest_layout_bundle(bundle["layout"], reconcile_native_text=False, pipeline_version="pipeline-v1")
    second = ingest_layout_bundle(bundle["layout"], reconcile_native_text=False, pipeline_version="pipeline-v2")
    with SQLiteRetrievalRecordStore() as store:
        store.replace(first)
        store.replace(second)
        assert store.counts(first.document.document_id, pipeline_version="pipeline-v1")["chunks"] == len(first.records)
        assert store.counts(second.document.document_id, pipeline_version="pipeline-v2")["chunks"] == len(second.records)
        hits = HybridRetriever(
            store,
            config=LayoutRAGConfig(pipeline_version="pipeline-v2"),
        ).retrieve("MOVS")
        assert hits
        assert {hit.record.pipeline_version for hit in hits} == {"pipeline-v2"}


def test_store_rejects_cross_version_records_before_mutation(tmp_path):
    result = _result(tmp_path)
    malformed_record = replace(result.records[0], pipeline_version="wrong-version")
    malformed = LayoutIngestionResult(
        result.document,
        result.sections,
        (malformed_record, *result.records[1:]),
    )
    with SQLiteRetrievalRecordStore() as store:
        with pytest.raises(ValueError, match="pipeline version"):
            store.replace(malformed)
        assert store.counts(result.document.document_id) == {
            "pages": 0,
            "regions": 0,
            "parents": 0,
            "chunks": 0,
        }


def test_parent_dense_hit_survives_without_literal_child_overlap(tmp_path):
    result = _result(tmp_path)
    target_parent = next(parent for parent in result.sections if parent.instruction_code == "MOVS")

    class ParentOnlyDenseIndex:
        def query_children(self, _query_embedding, *, limit):
            return ()

        def query_parents(self, _query_embedding, *, limit):
            return ((target_parent.parent_id, 1.0),)

    with SQLiteRetrievalRecordStore() as store:
        store.replace(result)
        hits = HybridRetriever(store, dense_index=ParentOnlyDenseIndex()).retrieve(
            "articulated interpolation",
            query_embedding=[1.0],
        )
        assert hits
        assert hits[0].record.parent_id == target_parent.parent_id


def test_fts_matches_reordered_noncontiguous_terms(tmp_path):
    result = _result(tmp_path)
    with SQLiteRetrievalRecordStore() as store:
        store.replace(result)
        assert store.search_lexical("command syntax")
        assert store.search_lexical("syntax motion")


def test_page_diversity_is_scoped_by_document(tmp_path):
    first = _result(tmp_path / "first")
    second_bundle = build_synthetic_bundle(tmp_path / "second" / "bundle", with_pdf=False)
    payload = json.loads(second_bundle["layout"].read_text(encoding="utf-8"))
    payload["pages"][0]["regions"][2]["content"] = "SX000002-A/1"
    payload["pages"][0]["regions"][2]["raw_content"] = "SX000002-A/1"
    second_bundle["layout"].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    second = ingest_layout_bundle(second_bundle["layout"], reconcile_native_text=False)
    assert first.document.document_id != second.document.document_id

    config = LayoutRAGConfig(result_limit=2, max_hits_per_parent=1, max_hits_per_page=1)
    with SQLiteRetrievalRecordStore() as store:
        store.replace(first)
        store.replace(second)
        hits = HybridRetriever(store, config=config).retrieve("Motion command exact syntax", limit=2)
        assert len(hits) == 2
        assert len({hit.record.document_id for hit in hits}) == 2


def test_page_visual_mapping_prefers_visual_evidence_and_config_is_validated(tmp_path):
    result = _result(tmp_path)
    page_asset_id = f"{result.document.document_id}:page-image:0002:150dpi"
    config = LayoutRAGConfig(max_visual_chunks_per_asset=2, result_limit=2)
    with SQLiteRetrievalRecordStore() as store:
        store.replace(result)
        hits = HybridRetriever(
            store,
            visual_retriever=lambda _query, _limit: [page_asset_id],
            config=config,
        ).retrieve("图示", limit=2)
        assert hits
        assert len(hits) <= 2
        assert all(hit.record.content_type in {"table_rows", "figure", "formula"} for hit in hits)

    with pytest.raises(ValueError, match="rrf_k"):
        LayoutRAGConfig(rrf_k=-1)
    with pytest.raises(ValueError, match="query bounds"):
        LayoutRAGConfig(max_query_terms=0)


def test_dense_validation_fails_before_mutating_store(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle", with_pdf=False)
    result = ingest_layout_bundle(
        bundle["layout"],
        reconcile_native_text=False,
        pipeline_version="pipeline-v2",
    )
    store = SQLiteRetrievalRecordStore()
    dense = ChromaDenseIndex(
        chromadb.EphemeralClient(),
        pipeline_version="pipeline-v1",
        embedding_model="fake-model",
    )

    with pytest.raises(ValueError, match="pipeline versions differ"):
        asyncio.run(
            HybridIndexer(store, dense).index(
                result,
                child_embeddings=[[1.0, 0.0]] * len(result.indexable_records),
                parent_embeddings=[[1.0, 0.0]]
                * len([parent for parent in result.sections if parent.summary and not parent.is_toc]),
            )
        )

    assert store.counts(result.document.document_id, pipeline_version="pipeline-v2") == {
        "pages": 0,
        "regions": 0,
        "parents": 0,
        "chunks": 0,
    }
    store.close()


def test_blank_limits_visual_mapping_and_reranker_injection_are_safe(tmp_path):
    result = _result(tmp_path)
    with SQLiteRetrievalRecordStore() as store:
        store.replace(result)
        assert HybridRetriever(store).retrieve("") == ()
        assert HybridRetriever(store).retrieve("MOVS", limit=0) == ()
        assert store.search_lexical("%") == ()

        table_asset = next(
            asset for asset in collect_region_visual_assets(result.document) if asset.asset_type == "table"
        )
        visual_hits = HybridRetriever(
            store,
            visual_retriever=lambda _query, _limit: [table_asset.asset_id],
        ).retrieve("图示 MOVS")
        assert any("visual" in hit.channel_ranks for hit in visual_hits)

        bad_record = next(record for record in result.records if not record.indexable)
        injected = SearchHit(record=bad_record, score=999.0)

        def unsafe_reranker(_query, hits):
            return [injected, hits[0], hits[0]]

        reranked = HybridRetriever(store, reranker=unsafe_reranker).retrieve("MOVS")
        assert len(reranked) == 1
        assert reranked[0].record.indexable


REFERENCE_STEM = "SX322023《新松机器人控制器软件指令集》(A-2)"
REFERENCE_LAYOUT = (
    Path(__file__).parents[1]
    / "assets"
    / "PDF-20260721"
    / ".md_unlimited"
    / "04指令手册"
    / f"{REFERENCE_STEM}.layout.json"
)
REFERENCE_PDF = (
    Path(__file__).parents[1]
    / "assets"
    / "PDF-20260721"
    / "04指令手册"
    / f"{REFERENCE_STEM}.pdf"
)


@pytest.mark.skipif(not (REFERENCE_LAYOUT.is_file() and REFERENCE_PDF.is_file()), reason="ignored reference bundle is unavailable")
def test_reference_exact_queries_retrieve_correct_instruction_and_citation():
    result = ingest_layout_bundle(REFERENCE_LAYOUT, pdf_path=REFERENCE_PDF)
    with SQLiteRetrievalRecordStore() as store:
        store.replace(result)
        retriever = HybridRetriever(store)
        expected = {
            "MOVJ": ("MOVJ", 42),
            "STRFINDEND": ("STRFINDEND", 23),
            "OUT_T": ("OUT_T", 40),
            "WAIT": ("WAIT", 61),
            "TCPRECV": ("TCPRECV", 70),
        }
        for query, (instruction, page) in expected.items():
            evidence = retriever.retrieve_evidence(query, limit=1)[0]
            assert evidence.hit.record.instruction_code == instruction
            assert evidence.hit.record.page_start in {page, page + 1}
            assert f"p.{evidence.hit.record.page_start}" in evidence.citation
