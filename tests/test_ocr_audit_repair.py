import asyncio
import json
from pathlib import Path

import fitz
from PIL import Image
from ribosome.preprocessing.ocr.audit_repair import (
    OCRFileQualityReport,
    OCRRegionQualityIssue,
    OCRSplitFragment,
    audit_layout_ocr_file,
    execute_ocr_repair_round,
    execute_ocr_repairs_until_stable,
    propose_ocr_repairs,
    repair_layout_regions_from_native_pdf,
    split_and_stitch_layout_regions,
)


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
