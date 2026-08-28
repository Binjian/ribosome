import asyncio
import json
import os
from pathlib import Path

import fitz
import pytest
from PIL import Image
from ribosome.preprocessing.ocr.audit_repair import (
    OCR_NATIVE_TEXT_REPAIR_ACTIONS,
    OCRFileQualityReport,
    OCRRegionQualityIssue,
    OCRSplitFragment,
    audit_layout_ocr_file,
    execute_ocr_repair_round,
    execute_ocr_repairs_until_stable,
    propose_ocr_repairs,
    repair_layout_regions_from_native_pdf,
    split_and_stitch_layout_regions,
    stage_layout_ocr_repairs,
    stitch_split_ocr_content,
)
from ribosome.preprocessing.ocr.utils import _source_signature


def _region(
    index: int,
    *,
    status: str,
    content: str = "",
    error: str | None = None,
    recovery: str | None = None,
    finish_reason: str | None = "stop",
    bbox: tuple[int, int, int, int] = (10, 10, 90, 90),
    asset: str | None = None,
    label: str = "text",
    task_type: str = "text",
) -> dict:
    return {
        "index": index,
        "label": label,
        "task_type": task_type,
        "status": status,
        "raw_content": content,
        "content": content,
        "error": error,
        "recovery": recovery,
        "finish_reason": finish_reason,
        "bbox": list(bbox),
        "asset": asset,
    }


def _write_layout(
    path: Path,
    regions: list[dict],
    *,
    status: str,
    page_status: str | None = None,
    width: int = 100,
    height: int = 100,
) -> None:
    path.write_text(
        json.dumps(
            {
                "status": status,
                "source": "source.pdf",
                "pages_total": 1,
                "pages": [
                    {
                        "page_number": 1,
                        "width": width,
                        "height": height,
                        "status": page_status or ("completed" if status == "processed" else "partial"),
                        "error": None,
                        "regions": regions,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_recovered_token_limit_is_verified_instead_of_split():
    issue = OCRRegionQualityIssue(
        page_number=1,
        region_index=1,
        label="table",
        task_type="table",
        recorded_status="recovered",
        issue_kind="recovered",
        reasons=("model output stopped at the token limit",),
        error=None,
        bbox=(0, 0, 100, 100),
        asset_path=None,
        content_preview="recovered table",
    )
    report = OCRFileQualityReport(
        sidecar_path=Path("recovered.layout.json"),
        source_path=Path("source.pdf"),
        document_status="processed",
        pages_total=1,
        partial_pages=(),
        file_reasons=(),
        region_issues=(issue,),
    )

    assert [proposal.action for proposal in propose_ocr_repairs([report])] == [
        "verify_recovery"
    ]


def test_native_text_dispatch_includes_split_repairs():
    assert set(OCR_NATIVE_TEXT_REPAIR_ACTIONS) == {
        "verify_recovery",
        "discard_and_reprocess",
        "split_and_stitch",
        "split_and_retry",
    }


def test_invalid_split_tile_is_subdivided_before_stitching(tmp_path):
    sidecar = tmp_path / "split.layout.json"
    asset_root = tmp_path / "split.assets"
    asset_root.mkdir()
    crop_path = asset_root / "table.png"
    Image.new("RGB", (100, 400), "white").save(crop_path)
    _write_layout(
        sidecar,
        [
            _region(
                1,
                status="failed",
                error="ValueError: output-token limit",
                finish_reason="length",
                bbox=(0, 0, 100, 400),
                asset="split.assets/table.png",
                label="table",
                task_type="table",
            )
        ],
        status="partial",
        width=100,
        height=400,
    )
    report = audit_layout_ocr_file(sidecar)
    proposals = tuple(
        proposal
        for proposal in propose_ocr_repairs([report])
        if proposal.action == "split_and_stitch"
    )
    calls: list[int] = []

    async def recognize(image, proposal, tile):
        calls.append(image.height)
        if image.height > 250:
            content = "<table><tr><td>invalid root</td></tr></table><|det|>"
        else:
            content = f"<table><tr><td>row {tile.top}</td></tr></table>"
        return OCRSplitFragment(content, content, "stop", {})

    message = asyncio.run(
        split_and_stitch_layout_regions(
            report,
            proposals,
            recognize,
            lambda layout: layout["pages"][0]["regions"][0]["content"],
            tile_height=500,
            overlap=40,
            boundary_search=20,
            min_tile_height=120,
            max_split_depth=2,
        )
    )

    repaired = json.loads(sidecar.read_text(encoding="utf-8"))
    assert calls[0] == 400
    assert len(calls) == 3
    assert "using 2 tile(s)" in message
    assert repaired["status"] == "processed"
    assert "<|det|>" not in repaired["pages"][0]["regions"][0]["content"]


def test_plain_text_table_fragments_are_stitched_with_overlap():
    stitched = stitch_split_ocr_content(
        [
            "Alarm code\nShared row",
            "Shared row\nResolution",
        ],
        task_type="table",
    )

    assert stitched == "Alarm code\nShared row\nResolution"


def test_native_pdf_best_effort_skips_region_without_aligned_text(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=200, height=200)
        page.insert_text((20, 40), "usable native text")
        pdf.save(source)

    sidecar = tmp_path / "native.layout.json"
    _write_layout(
        sidecar,
        [
            _region(
                1,
                status="recovered",
                content="usable native text",
                recovery="recovered prefix",
                bbox=(10, 20, 190, 55),
            ),
            _region(
                2,
                status="recovered",
                content="image-only note",
                recovery="recovered prefix",
                bbox=(10, 120, 190, 170),
            ),
        ],
        status="processed",
        width=200,
        height=200,
    )
    report = audit_layout_ocr_file(sidecar)

    message = repair_layout_regions_from_native_pdf(
        report,
        propose_ocr_repairs([report]),
        source,
        lambda layout: "rendered markdown",
        skip_unavailable_native_text=True,
    )

    repaired = json.loads(sidecar.read_text(encoding="utf-8"))
    first, second = repaired["pages"][0]["regions"]
    assert "repaired 1 region(s)" in message
    assert "unavailable=1" in message
    assert first["status"] == "completed"
    assert second["status"] == "recovered"
    assert second["content"] == "image-only note"


def test_native_pdf_repairs_split_proposal_and_cleans_workspace(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=200, height=200)
        page.insert_text((20, 40), "alarm code resolution")
        pdf.save(source)

    sidecar = tmp_path / "native-split.layout.json"
    _write_layout(
        sidecar,
        [
            _region(
                1,
                status="failed",
                error="ValueError: output-token limit",
                finish_reason="length",
                bbox=(10, 20, 190, 55),
                label="table",
                task_type="table",
            )
        ],
        status="partial",
        width=200,
        height=200,
    )
    work_root = tmp_path / ".native-split.layout-work"
    work_root.mkdir()
    (work_root / "checkpoint.json").write_text(
        sidecar.read_text(encoding="utf-8"), encoding="utf-8"
    )
    partial_markdown = tmp_path / "native-split.partial.md"
    partial_sidecar = tmp_path / "native-split.partial.layout.json"
    partial_markdown.write_text("partial", encoding="utf-8")
    partial_sidecar.write_text("{}", encoding="utf-8")
    report = audit_layout_ocr_file(sidecar)
    proposals = propose_ocr_repairs([report])

    assert any(proposal.action == "split_and_stitch" for proposal in proposals)
    message = repair_layout_regions_from_native_pdf(
        report,
        proposals,
        source,
        lambda layout: layout["pages"][0]["regions"][0]["content"],
    )

    repaired = json.loads(sidecar.read_text(encoding="utf-8"))
    assert "repaired 1 region(s)" in message
    assert repaired["status"] == "processed"
    assert repaired["pages"][0]["regions"][0]["content"] == "alarm code resolution"
    assert not work_root.exists()
    assert not partial_markdown.exists()
    assert not partial_sidecar.exists()


def test_checkpoint_staging_archives_each_mutating_region_exactly_once(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"checkpoint source")
    source_stat = source.stat()
    sidecar = tmp_path / "history.layout.json"
    assets = tmp_path / "history.assets"
    assets.mkdir()
    (assets / "decorative.png").write_bytes(b"crop")
    _write_layout(
        sidecar,
        [
            _region(
                1,
                status="failed",
                content="failed retry",
                error="backend unavailable",
            ),
            _region(2, status="pending", content="interrupted region"),
            _region(
                3,
                status="failed",
                content="missing crop retry",
                error="backend unavailable",
                asset="history.assets/missing.png",
            ),
            _region(
                4,
                status="completed",
                content=(
                    "The Ground Truth image differs. According to Rule 2, "
                    "the provided OCR content must be rejected."
                ),
            ),
            _region(
                5,
                status="preserved",
                recovery="Unlimited-OCR returned no usable text after retry",
                finish_reason=None,
                asset="history.assets/decorative.png",
                label="number",
            ),
            _region(6, status="pending", content="document-resume fallback"),
        ],
        status="partial",
    )
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["source"] = str(source)
    payload["source_signature"] = {
        "path": str(source),
        "size": source_stat.st_size,
        "mtime_ns": source_stat.st_mtime_ns,
    }
    for region in payload["pages"][0]["regions"]:
        region["raw_content"] = f"raw response {region['index']}"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    report = audit_layout_ocr_file(sidecar)
    all_proposals = propose_ocr_repairs([report])
    region_proposals = {
        proposal.region_index: proposal
        for proposal in all_proposals
        if proposal.scope == "region"
    }
    proposals = [
        proposal
        for proposal in all_proposals
        if not (proposal.scope == "region" and proposal.region_index == 6)
    ]

    staged = stage_layout_ocr_repairs(report, proposals, source)
    regions = {
        region["index"]: region for region in staged["pages"][0]["regions"]
    }

    for index in range(1, 7):
        assert len(regions[index]["repair_history"]) == 1
        assert regions[index]["repair_history"][0]["raw_content"] == (
            f"raw response {index}"
        )
    for index in range(1, 6):
        history = regions[index]["repair_history"][0]
        assert history["action"] == region_proposals[index].action
        assert history["reasons"] == list(region_proposals[index].reasons)

    fallback = regions[6]["repair_history"][0]
    assert fallback["action"] == "resume_document"
    assert fallback["reasons"] == list(
        dict.fromkeys(
            reason
            for proposal in all_proposals
            if proposal.scope == "file" and proposal.action == "resume_document"
            for reason in proposal.reasons
        )
    )
    assert regions[1]["status"] == "failed"
    assert regions[2]["status"] == "pending"
    assert regions[3]["status"] == "failed"
    assert regions[4]["status"] == "pending"
    assert regions[4]["content"] == ""
    assert regions[5]["status"] == "recovered"
    assert regions[6]["status"] == "pending"
    assert staged["source_signature"]["sha256"] == _source_signature(source)[
        "sha256"
    ]

    relocated = tmp_path / "relocated.pdf"
    relocated.write_bytes(source.read_bytes())
    os.utime(
        relocated,
        ns=(source_stat.st_mtime_ns, source_stat.st_mtime_ns),
    )
    relocated_staged = stage_layout_ocr_repairs(
        report, proposals, relocated
    )
    assert relocated_staged["source_signature"] == _source_signature(relocated)

    checkpoint = tmp_path / ".history.layout-work" / "checkpoint.json"
    legacy_relocated = json.loads(checkpoint.read_text(encoding="utf-8"))
    legacy_relocated["source_signature"].pop("sha256")
    checkpoint.write_text(json.dumps(legacy_relocated), encoding="utf-8")
    second_move = tmp_path / "second-move.pdf"
    second_move.write_bytes(relocated.read_bytes())
    os.utime(
        second_move,
        ns=(source_stat.st_mtime_ns, source_stat.st_mtime_ns),
    )
    with pytest.raises(ValueError, match="Source signature changed"):
        stage_layout_ocr_repairs(report, proposals, second_move)

    hashed_checkpoint = json.loads(checkpoint.read_text(encoding="utf-8"))
    hashed_checkpoint["source_signature"] = _source_signature(relocated)
    checkpoint.write_text(json.dumps(hashed_checkpoint), encoding="utf-8")
    relocated.write_bytes(b"checkpoint sourcf")
    os.utime(
        relocated,
        ns=(source_stat.st_mtime_ns, source_stat.st_mtime_ns),
    )
    with pytest.raises(ValueError, match="Source signature changed"):
        stage_layout_ocr_repairs(report, proposals, relocated)


def test_partial_document_scope_excludes_processed_advisories(tmp_path):
    partial = tmp_path / "partial.layout.json"
    processed = tmp_path / "processed.layout.json"
    _write_layout(
        partial,
        [_region(1, status="failed", error="backend unavailable")],
        status="partial",
    )
    _write_layout(
        processed,
        [
            _region(
                1,
                status="recovered",
                content="usable recovery",
                recovery="recovered prefix",
            )
        ],
        status="processed",
    )

    async def repair(report, proposals):
        payload = json.loads(report.sidecar_path.read_text(encoding="utf-8"))
        payload["status"] = "processed"
        payload["pages"][0]["status"] = "completed"
        payload["pages"][0]["regions"][0].update(
            status="completed", content="fixed", error=None
        )
        report.sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
        return "fixed partial document"

    result = asyncio.run(
        execute_ocr_repairs_until_stable(
            tmp_path,
            {"resume_document": repair, "retry_region": repair},
            partial_documents_only=True,
            show_progress=False,
            stream_reports=False,
        )
    )

    assert result.clean
    assert result.stop_reason == "clean"
    assert len(result.rounds) == 1
    assert result.rounds[0].attempts[0].sidecar_path == partial.resolve()
    assert audit_layout_ocr_file(processed).needs_repair


def test_streamed_handler_failure_includes_exception(tmp_path, capsys):
    sidecar = tmp_path / "failed.layout.json"
    _write_layout(
        sidecar,
        [_region(1, status="failed", error="backend unavailable")],
        status="partial",
    )
    report = audit_layout_ocr_file(sidecar)

    async def fail(report, proposals):
        raise RuntimeError("precise backend failure")

    asyncio.run(
        execute_ocr_repair_round(
            [report],
            {"resume_document": fail, "retry_region": fail},
            show_progress=False,
            stream_reports=True,
        )
    )

    assert "RuntimeError: precise backend failure" in capsys.readouterr().out
