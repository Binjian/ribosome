import copy
import json

import pytest
from ribosome.preprocessing.conditioning import markdown as markdown_conditioning
from ribosome.preprocessing.conditioning.markdown import (
    MarkdownConditioningConfig,
    SemanticsEligibilityReport,
    condition_layout_markdown,
    condition_markdown_file,
    condition_markdown_folder,
)
from ribosome.preprocessing.ocr.layout_bundle import LayoutBundleValidationError


def _region() -> dict:
    return {
        "index": 1,
        "label": "text",
        "bbox": [20, 20, 480, 100],
        "task_type": "text",
        "status": "completed",
        "asset": None,
        "raw_content": "Conditioning input.",
        "content": "Conditioning input.",
        "error": None,
        "finish_reason": "stop",
    }


def _layout() -> dict:
    return {
        "schema_version": 2,
        "status": "processed",
        "source": "manual.pdf",
        "pages_total": 1,
        "pages": [
            {
                "page_number": 1,
                "width": 500,
                "height": 700,
                "status": "completed",
                "error": None,
                "regions": [_region()],
            }
        ],
    }


def _markdown() -> str:
    return (
        "<!-- Page 1 -->\n\n"
        "<!-- layout-region page=1 index=1 label=text "
        "bbox=20,20,480,100 status=completed -->\n\n"
        "Conditioning input.\n"
    )


def _write_bundle(root, *, status: str = "processed"):
    source = root / "manual.md"
    sidecar = root / "manual.layout.json"
    layout = _layout()
    layout["status"] = status
    source.write_text(_markdown(), encoding="utf-8")
    sidecar.write_text(json.dumps(layout), encoding="utf-8")
    return source, sidecar


def test_non_strict_file_conditioning_reports_bundle_issues_before_ocr_warnings(
    tmp_path, monkeypatch
):
    source, sidecar = _write_bundle(tmp_path, status="unexpected")
    monkeypatch.setattr(
        markdown_conditioning,
        "validate_semantics_ready_markdown",
        lambda *_args, **_kwargs: SemanticsEligibilityReport(
            True,
            {},
            (),
            0,
            0,
            0,
            warnings=("Pandoc warning",),
        ),
    )

    result = condition_markdown_file(
        source,
        layout_path=sidecar,
        config=MarkdownConditioningConfig(strict=False),
    )

    assert result.report.eligible
    assert result.report.warnings[:3] == (
        "Bundle validation [error:unknown_bundle_status]: unknown bundle status 'unexpected'",
        "OCR audit: document status is 'unexpected'",
        "Pandoc warning",
    )


def test_strict_file_conditioning_does_not_write_after_bundle_validation_failure(tmp_path):
    source, sidecar = _write_bundle(tmp_path, status="unexpected")
    output = tmp_path / "manual.conditioned.md"

    with pytest.raises(LayoutBundleValidationError):
        condition_markdown_file(source, layout_path=sidecar, output_path=output)

    assert not output.exists()


def test_strict_file_conditioning_does_not_write_after_ocr_audit_failure(tmp_path):
    source, sidecar = _write_bundle(tmp_path)
    layout = json.loads(sidecar.read_text(encoding="utf-8"))
    commentary = (
        "The Ground Truth image differs. According to Rule 2, "
        "the provided OCR content must be rejected."
    )
    layout["pages"][0]["regions"][0].update(
        raw_content=commentary,
        content=commentary,
    )
    sidecar.write_text(json.dumps(layout), encoding="utf-8")
    output = tmp_path / "manual.conditioned.md"

    with pytest.raises(ValueError, match="layout OCR audit has unresolved findings"):
        condition_markdown_file(source, layout_path=sidecar, output_path=output)

    assert not output.exists()


@pytest.mark.parametrize(
    ("markdown_mutator", "layout_mutator", "message"),
    [
        (
            lambda markdown: markdown + markdown.split("\n\n", 1)[1],
            lambda layout: None,
            "duplicate Markdown marker p1r1",
        ),
        (
            lambda markdown: markdown,
            lambda layout: layout["pages"][0]["regions"].append(
                copy.deepcopy(layout["pages"][0]["regions"][0])
            ),
            "duplicate layout region p1r1",
        ),
        (
            lambda markdown: markdown,
            lambda layout: layout["pages"].append(copy.deepcopy(layout["pages"][0])),
            "duplicate layout page p1",
        ),
    ],
)
def test_strict_in_memory_conditioning_rejects_duplicate_keys(
    markdown_mutator, layout_mutator, message
):
    layout = _layout()
    layout_mutator(layout)

    with pytest.raises(ValueError, match=message):
        condition_layout_markdown(markdown_mutator(_markdown()), layout)


def test_clean_folder_conditioning_has_clean_status(tmp_path):
    _write_bundle(tmp_path)

    result = condition_markdown_folder(
        tmp_path,
        show_progress=False,
        print_errors=False,
    )

    assert result.status_counts == {"conditioned": 1}
    assert result.items[0].report is not None
    assert result.items[0].report.eligible
    assert result.items[0].report.warnings == ()
