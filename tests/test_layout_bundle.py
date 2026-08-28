import json
from pathlib import Path

import pytest
from layout_bundle_factory import build_synthetic_bundle
from ribosome.preprocessing.ocr.audit_repair import audit_layout_ocr_file
from ribosome.preprocessing.ocr.layout_bundle import (
    LayoutBundlePaths,
    LayoutBundleValidationError,
    LayoutBundleValidator,
    OCRQualityGate,
    PDFPageRenderer,
    collect_region_visual_assets,
    ingest_layout_bundle,
    normalize_html_table,
    pair_layout_bundles,
    parse_markdown_regions,
    sha256_file,
)


def test_quality_gate_accepts_intentional_empty_decorative_regions():
    flags = OCRQualityGate().initial_flags(
        {
            "label": "number",
            "task_type": "text",
            "status": "recovered",
            "content": "",
            "recovery": (
                "Discarded a degenerate Unlimited-OCR response for a "
                "decorative or empty number region"
            ),
            "finish_reason": "stop",
        }
    )

    assert not any(flag.code == "empty_content" for flag in flags)


def test_ingestion_joins_markers_hashes_pdf_and_repairs_suspicious_ocr(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle")

    result = ingest_layout_bundle(
        bundle["layout"],
        markdown_path=bundle["markdown"],
        pdf_path=bundle["pdf"],
    )

    document = result.document
    assert document.document_id == f"sha256:{sha256_file(bundle['pdf'])}"
    assert len(parse_markdown_regions(bundle["markdown"].read_text(encoding="utf-8"))) == 9
    assert len(document.regions) == 9
    assert len(document.artifact_hashes) == 5  # PDF, layout, Markdown, and two crops.
    suspicious = next(region for region in document.regions if region.key == (2, 5))
    assert suspicious.text_source == "native_pdf_recovery"
    assert "WAIT IO T=5" in suspicious.canonical_text
    assert not suspicious.quarantined
    assert {flag.code for flag in suspicious.quality_flags} >= {
        "suspicious_completed",
        "native_pdf_recovery",
    }
    assert all(flag.resolved for flag in suspicious.quality_flags if flag.severity == "error")
    repaired_record = next(record for record in result.records if suspicious.region_id in record.region_ids)
    flag_details = repaired_record.region_quality_flags[repaired_record.region_ids.index(suspicious.region_id)]
    assert any(flag["code"] == "suspicious_completed" and flag["resolved"] for flag in flag_details)
    assert len(repaired_record.source_hashes) <= 4


def test_fallback_document_id_is_path_independent(tmp_path):
    first = build_synthetic_bundle(tmp_path / "first", with_pdf=False)
    second = build_synthetic_bundle(tmp_path / "second", with_pdf=False)

    first_result = ingest_layout_bundle(first["layout"], reconcile_native_text=False)
    second_result = ingest_layout_bundle(second["layout"], reconcile_native_text=False)

    assert first_result.document.document_id == second_result.document.document_id
    assert first_result.document.artifact_hashes["layout"] != second_result.document.artifact_hashes["layout"]


def test_validator_rejects_marker_mismatch_and_missing_asset(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle", with_pdf=False)
    markdown = bundle["markdown"].read_text(encoding="utf-8")
    bundle["markdown"].write_text(markdown.replace("label=table", "label=text", 1), encoding="utf-8")
    (bundle["assets"] / "page-0002-region-001-table.png").unlink()

    with pytest.raises(LayoutBundleValidationError) as caught:
        LayoutBundleValidator().validate(LayoutBundlePaths.from_layout(bundle["layout"]))

    codes = {issue.code for issue in caught.value.issues}
    assert {"marker_mismatch", "missing_asset"} <= codes


def test_validator_accepts_processed_and_rejects_asset_traversal(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle", with_pdf=False)
    payload = json.loads(bundle["layout"].read_text(encoding="utf-8"))
    payload["status"] = "processed"
    payload["pages"][1]["regions"][0]["asset"] = "../outside.png"
    (tmp_path / "outside.png").write_bytes(b"outside")
    bundle["layout"].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(LayoutBundleValidationError) as caught:
        LayoutBundleValidator().validate(LayoutBundlePaths.from_layout(bundle["layout"]))

    assert "unknown_bundle_status" not in {issue.code for issue in caught.value.issues}
    assert "unsafe_asset_path" in {issue.code for issue in caught.value.issues}


def test_validator_rejects_asset_prefix_escape_and_accepts_formula_crops(tmp_path):
    unsafe = build_synthetic_bundle(tmp_path / "unsafe", with_pdf=False)
    payload = json.loads(unsafe["layout"].read_text(encoding="utf-8"))
    payload["asset_prefix"] = "../outside"
    payload["pages"][1]["regions"][0]["asset"] = "table.png"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "table.png").write_bytes(b"outside")
    unsafe["layout"].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(LayoutBundleValidationError) as caught:
        LayoutBundleValidator().validate(LayoutBundlePaths.from_layout(unsafe["layout"]))
    assert "unsafe_asset_prefix" in {issue.code for issue in caught.value.issues}

    formula = build_synthetic_bundle(tmp_path / "formula", with_pdf=False)
    payload = json.loads(formula["layout"].read_text(encoding="utf-8"))
    region = payload["pages"][1]["regions"][1]
    region.update(label="formula", task_type="formula", status="completed", content="x = y + 1", raw_content="x = y + 1")
    formula["layout"].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    markdown = formula["markdown"].read_text(encoding="utf-8")
    markdown = markdown.replace("label=image bbox=100,100,200,180 status=preserved", "label=formula bbox=100,100,200,180 status=completed")
    markdown = markdown.replace("![asset](<SX000001-layout-test(A-1).assets/page-0002-region-002-image.png>)", "![asset](<SX000001-layout-test(A-1).assets/page-0002-region-002-image.png>)\n\nx = y + 1")
    formula["markdown"].write_text(markdown, encoding="utf-8")

    result = ingest_layout_bundle(formula["layout"], reconcile_native_text=False)
    assert any(record.content_type == "formula" and record.exact_text == "x = y + 1" for record in result.records)
    assert any(asset.asset_type == "formula" for asset in collect_region_visual_assets(result.document))


def test_partial_pairing_uses_partial_markdown_and_final_document_stem(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle", with_pdf=False)
    base = bundle["layout"].name[: -len(".layout.json")]
    partial_layout = bundle["layout"].with_name(f"{base}.partial.layout.json")
    partial_markdown = bundle["markdown"].with_name(f"{base}.partial.md")
    bundle["layout"].rename(partial_layout)
    bundle["markdown"].rename(partial_markdown)

    assert pair_layout_bundles(tmp_path / "bundle") == ()
    paired = pair_layout_bundles(tmp_path / "bundle", include_partial=True)
    assert len(paired) == 1
    assert paired[0].markdown_path == partial_markdown.resolve()
    assert paired[0].stem == base


def test_hierarchy_carries_parent_across_pages_and_suppresses_nested_figure(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle", with_pdf=False)
    result = ingest_layout_bundle(bundle["layout"], reconcile_native_text=False)
    regions = {region.key: region for region in result.document.regions}

    assert regions[(1, 1)].section_number == "4.4.4"
    assert regions[(2, 1)].parent_id == regions[(1, 1)].parent_id
    assert regions[(2, 2)].nested_parent_region_id == regions[(2, 1)].region_id
    figure_record = next(record for record in result.records if record.region_ids == (regions[(2, 2)].region_id,))
    assert not figure_record.indexable
    regions_by_id = result.document.region_map
    assert all(
        len({regions_by_id[region_id].parent_id for region_id in record.region_ids}) == 1
        for record in result.records
    )


def test_table_normalization_expands_spans_and_preserves_parameter_literals():
    table = normalize_html_table(
        '<table><tr><td rowspan="2">MOVJ</td><td>格式</td>'
        '<td>MOVJ P&lt;参数 1&gt;</td></tr><tr><td colspan="2">范围 1-100</td></tr></table>'
    )

    assert table.rows == (
        ("MOVJ", "格式", "MOVJ P<参数 1>"),
        ("MOVJ", "范围 1-100", "范围 1-100"),
    )
    assert "<参数 1>" in table.text
    assert normalize_html_table('<table><tr><td rowspan="bad">safe</td></tr></table>').text == "safe"


def test_native_syntax_conflict_is_quarantined_without_overwriting_ocr(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle")
    payload = json.loads(bundle["layout"].read_text(encoding="utf-8"))
    source_region = payload["pages"][0]["regions"][1]
    source_region["content"] = source_region["raw_content"] = "OUT_T != IO"
    bundle["layout"].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = ingest_layout_bundle(bundle["layout"], pdf_path=bundle["pdf"])
    region = next(region for region in result.document.regions if region.key == (1, 2))

    assert region.canonical_text == "OUT_T != IO"
    assert region.text_source == "ocr+native_pdf_conflict"
    assert region.quarantined
    assert any(flag.code == "ocr_native_syntax_conflict" and not flag.resolved for flag in region.quality_flags)


def test_visual_assets_render_pages_and_suppress_nested_crops(tmp_path):
    bundle = build_synthetic_bundle(tmp_path / "bundle")
    result = ingest_layout_bundle(bundle["layout"], pdf_path=bundle["pdf"])

    page_assets = PDFPageRenderer(dpi=72).render(
        result.document,
        tmp_path / "pages",
        page_numbers=[2],
    )
    crop_assets = collect_region_visual_assets(result.document)

    assert len(page_assets) == 1
    assert page_assets[0].path.is_file()
    assert page_assets[0].page_number == 2
    assert any(asset.asset_type == "table" and asset.indexable for asset in crop_assets)
    assert any(asset.asset_type == "figure" and not asset.indexable for asset in crop_assets)


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
def test_reference_bundle_acceptance_counts_and_known_repairs():
    audit = audit_layout_ocr_file(REFERENCE_LAYOUT)
    assert not audit.needs_repair

    sidecar = json.loads(REFERENCE_LAYOUT.read_text(encoding="utf-8"))
    sidecar_regions = {
        (page["page_number"], region["index"]): region
        for page in sidecar["pages"]
        for region in page["regions"]
    }
    repair_strategies = {
        (5, 5): "verified_existing_ocr",
        (7, 14): "verified_existing_ocr",
        (8, 2): "verified_existing_ocr",
        (53, 4): "native_pdf_replacement",
        (61, 7): "native_pdf_replacement",
        (66, 2): "native_pdf_replacement",
        (70, 4): "verified_existing_ocr",
    }
    for key, strategy in repair_strategies.items():
        assert len(sidecar_regions[key]["repair_history"]) == 1
        assert sidecar_regions[key]["native_text_repair"]["strategy"] == strategy
    assert len(sidecar_regions[(9, 5)]["repair_history"]) == 1
    assert sidecar_regions[(9, 5)]["status"] == "recovered"

    result = ingest_layout_bundle(REFERENCE_LAYOUT, pdf_path=REFERENCE_PDF)

    assert len(result.document.pages) == 72
    assert len(result.document.regions) == 731
    assert sum(region.task_type == "table" for region in result.document.regions) == 152
    assert sum(region.asset_path is not None for region in result.document.regions) == 167
    assert len(result.document.artifact_hashes) == 170
    assert result.document.document_title == "新松机器人控制器软件指令集"
    assert len(result.sections) == 98
    assert sum(section.number is not None for section in result.sections) == 96
    regions = {region.key: region for region in result.document.regions}
    assert regions[(21, 5)].section_number == "4.1.10"
    assert regions[(21, 5)].instruction_code == "STRSUB"
    assert regions[(66, 2)].section_number == "4.5.12"
    assert regions[(66, 2)].instruction_code == "SWITCH"
    for key in ((53, 4), (61, 7), (66, 2)):
        region = next(region for region in result.document.regions if region.key == key)
        assert region.text_source == "native_pdf"
        assert not region.quarantined
        assert not region.quality_flags
    assert regions[(70, 4)].text_source == "native_pdf"
    assert not any(region.quarantined for region in result.document.regions)
    sections = {section.number: section for section in result.sections}
    assert sections["4.1.8"].title == sections["4.1.8"].instruction_code == "STRLEN"
    assert sections["4.1.14"].title == sections["4.1.14"].instruction_code == "STRREVERSE"
    assert sections["4.3"].title == "I/O 指令"
    assert sections["4.3"].instruction_code is None
    assert sections["4.5.8"].instruction_code == "START BG"
    assert sections["4.6.3"].instruction_code == "TIMER START"
    assert sections["4.6.4"].instruction_code == "TIMER STOP"
    assert sections["4.6.9"].instruction_code == "USER_OFFSET_CONDITION"
    assert sections["4.6.10"].instruction_code == "TOOL_OFFSET_CONDITION"
    for key in ((56, 10), (58, 4), (71, 2), (71, 4), (10, 25), (11, 2), (48, 5)):
        region = regions[key]
        assert region.text_source == "native_pdf_correction"
        assert not region.quarantined
