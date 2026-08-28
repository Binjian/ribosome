import asyncio
import json

import pytest
import ribosome.application.layout_rag as application_module
from layout_bundle_factory import build_synthetic_bundle
from ribosome.application.layout_rag import (
    LayoutRAGApplication,
    OCRAuditBlockedError,
)
from ribosome.retrieval.layout_rag import SQLiteRetrievalRecordStore

_SUSPICIOUS_TEXT = (
    "The Ground Truth image is clean. According to Rule 2, this is valid. "
    "Provided OCR content follows but is evaluator leakage."
)


def _make_audit_clean(bundle):
    payload = json.loads(bundle["layout"].read_text(encoding="utf-8"))
    payload["status"] = "processed"
    repaired = payload["pages"][1]["regions"][4]
    repaired["raw_content"] = "WAIT IO T=5"
    repaired["content"] = "WAIT IO T=5"
    bundle["layout"].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown = bundle["markdown"].read_text(encoding="utf-8")
    bundle["markdown"].write_text(
        markdown.replace(_SUSPICIOUS_TEXT, "WAIT IO T=5"),
        encoding="utf-8",
    )
    return bundle


def test_application_blocks_unclean_bundle_before_ingestion(tmp_path, monkeypatch):
    bundle = build_synthetic_bundle(tmp_path / "dirty", with_pdf=False)

    def forbidden_ingestion(*_args, **_kwargs):
        raise AssertionError("ingestion must not run before the audit gate passes")

    monkeypatch.setattr(application_module, "ingest_layout_bundle", forbidden_ingestion)
    with SQLiteRetrievalRecordStore() as store:
        app = LayoutRAGApplication(store)
        with pytest.raises(OCRAuditBlockedError) as caught:
            asyncio.run(
                app.index_bundle(
                    bundle["layout"],
                    reconcile_native_text=False,
                )
            )

        report = caught.value.report
        assert report.file_reasons == ("document status is 'completed'",)
        assert [(issue.page_number, issue.region_index, issue.issue_kind) for issue in report.region_issues] == [
            (2, 5, "suspicious_completed")
        ]
        for table in ("documents", "pages", "regions", "parents", "chunks"):
            assert store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_application_runs_lexical_pipeline_and_cited_generation(tmp_path):
    bundle = _make_audit_clean(
        build_synthetic_bundle(tmp_path / "clean", with_pdf=False)
    )
    answer_requests = []

    async def answer_generator(request):
        answer_requests.append(request)
        return "Use the retrieved MOVS syntax [E1]."

    with SQLiteRetrievalRecordStore() as store:
        app = LayoutRAGApplication(store, answer_generator=answer_generator)
        indexed = asyncio.run(
            app.index_bundle(
                bundle["layout"],
                reconcile_native_text=False,
            )
        )

        assert not indexed.audit.needs_repair
        assert indexed.repair is None
        assert indexed.counts == {
            "pages": 2,
            "regions": 9,
            "parents": 5,
            "chunks": 6,
        }

        query = "MOVS P<参数 1> OUT_T IO"
        result = asyncio.run(app.query(query, limit=1))
        assert result.answerable
        assert result.evidence[0].hit.record.instruction_code == "MOVS"
        assert "lexical" in result.evidence[0].hit.channel_ranks
        assert result.context.startswith("[E1] [SX000001 A/1,")
        citation = result.citations[0]
        assert citation["region_ids"]
        assert citation["bboxes"]
        assert citation["source_layout_uri"].endswith(".layout.json")
        assert citation["source_hashes"]["layout"]

        answer = asyncio.run(app.answer(query, limit=1))
        assert answer.text == "Use the retrieved MOVS syntax [E1]."
        assert not answer.abstained
        assert answer_requests[0].context.startswith("[E1]")
        assert answer_requests[0].citations[0]["bboxes"]
