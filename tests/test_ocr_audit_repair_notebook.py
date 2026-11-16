"""Exercise the application configuration and native-crop dispatch offline."""
import ast
import asyncio
import json
from pathlib import Path

from PIL import Image
from ribosome.preprocessing.ocr import audit_repair as audit
from ribosome.preprocessing.ocr import baidu_unlimited as ocr


NOTEBOOK = Path(__file__).resolve().parents[1] / "nbs/03.preprocessing.ocr.audit_repair.ipynb"


def offline_self_check():
    from fastcore.test import test_eq
    from tempfile import TemporaryDirectory

    notebook = json.loads(NOTEBOOK.read_text())
    source = "".join(notebook["cells"][22]["source"])
    namespace = dict(vars(audit))
    namespace.update(test_eq=test_eq, TemporaryDirectory=TemporaryDirectory)
    code = compile(source, str(NOTEBOOK), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    return namespace, code


def test_offline_self_checks_report_pass_without_simulated_failure_logs(monkeypatch, capsys):
    import socket

    def forbidden_network(*args, **kwargs):
        raise AssertionError("Offline notebook checks must not contact the OCR service")

    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    namespace, code = offline_self_check()
    asyncio.run(eval(code, namespace))
    output = capsys.readouterr()
    assert output.out == "PASS: offline OCR repair self-checks (including expected error-handling cases).\n"
    assert "OCR repair" not in output.err


def test_offline_self_checks_still_raise_unexpected_failures(capsys):
    import pytest

    namespace, code = offline_self_check()

    async def unexpected_failure(*args, **kwargs):
        raise RuntimeError("unexpected self-check defect")

    namespace["execute_ocr_repair_round"] = unexpected_failure
    with pytest.raises(RuntimeError, match="unexpected self-check defect"):
        asyncio.run(eval(code, namespace))
    assert "PASS:" not in capsys.readouterr().out


def application(root):
    notebook = json.loads(NOTEBOOK.read_text())
    namespace = dict(vars(audit))
    namespace.update(PROJ_ROOT=root, OCR_SOURCE_ROOT=root / "assets/pdf.remaining",
                     OCR_AUDIT_ROOT=root / "assets/pdf.remaining/.md_unlimited_v2",
                     OCR_REPAIR_TARGET=None, PARTIAL_DOCUMENTS_ONLY=False,
                     ocr_repair_queue=[])
    tree = ast.parse("".join(notebook["cells"][18]["source"]))
    end = next(i for i, node in enumerate(tree.body)
               if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "ocr_repair_execution" for t in node.targets))
    exec(compile(ast.Module(body=tree.body[:end], type_ignores=[]), str(NOTEBOOK), "exec"), namespace)
    return namespace


def test_native_processed_bundle_repairs_crop_without_layout_detector(tmp_path):
    root = tmp_path / "assets/pdf.remaining"
    output = root / ".md_unlimited_v2"
    output.mkdir(parents=True)
    source = root / "sample.png"
    Image.new("RGB", (100, 100), "white").save(source)
    assets = output / "sample.assets"
    assets.mkdir()
    Image.new("RGB", (80, 80), "white").save(assets / "crop.png")
    sidecar = output / "sample.layout.json"
    payload = {
        "schema_version": 2, "status": "processed", "source": str(source),
        "source_signature": audit._source_signature(source), "pages_total": 1,
        "settings": {"layout_mode": "native", "layout_model": "baidu/Unlimited-OCR-2",
                     "model": "Unlimited-OCR-2", "ngram_window": None,
                     "prompt": "Multi page parsing.", "crop_mode": True},
        "native": {"raw_content": "Original document evidence", "merge_groups": []},
        "pages": [{"page_number": 1, "width": 100, "height": 100, "status": "completed",
                   "regions": [{"index": 1, "label": "other", "task_type": "text",
                                "bbox": [10, 10, 90, 90], "status": "preserved", "content": "",
                                "raw_content": "", "asset": "sample.assets/crop.png",
                                "recovery": "Empty native region preserved for review", "error": None}]}],
    }
    sidecar.write_text(json.dumps(payload))
    (output / "sample.md").write_text("Old Markdown")
    namespace = application(tmp_path)
    namespace["ocr_repair_queue"] = audit.audit_layout_ocr_results(output)
    calls = []

    def forbidden_detector(**kwargs):
        raise AssertionError("Native audit repairs must not create a PP-DocLayout detector")

    async def fake_parse(*args, **kwargs):
        calls.append(kwargs)
        return "Recovered crop text", {"raw_content": "Recovered crop text", "finish_reason": "stop"}

    class Client:
        def __init__(self, **kwargs):
            self.transport = kwargs["http_client"]

        async def close(self):
            await self.transport.aclose()

    namespace.update(create_pp_doclayout_detector=forbidden_detector, AsyncOpenAI=Client, _parse_pixmap=fake_parse)
    result = asyncio.run(namespace["run_checkpoint_repairs"](execute=True))
    assert result.stop_reason == "clean"
    assert len(calls) == 1 and calls[0]["ngram_window"] is None
    repaired = json.loads(sidecar.read_text())
    assert repaired["settings"] == payload["settings"]
    assert repaired["native"] == payload["native"]
    assert repaired["pages"][0]["regions"][0]["content"] == "Recovered crop text"
    assert repaired["pages"][0]["regions"][0]["repair_history"]
    assert "Recovered crop text" in (output / "sample.md").read_text()


def test_corpus_configuration_selects_processed_findings(tmp_path, monkeypatch):
    notebook = json.loads(NOTEBOOK.read_text())
    namespace = dict(vars(audit))
    from ribosome.preprocessing.ocr import utils
    monkeypatch.setattr(utils, "find_project_root", lambda: tmp_path)
    expected = tmp_path / "assets/pdf.remaining/.md_unlimited_v2"
    expected.mkdir(parents=True)
    assets = expected / "flagged.assets"
    assets.mkdir()
    Image.new("RGB", (80, 80), "white").save(assets / "crop.png")
    (expected / "flagged.layout.json").write_text(json.dumps({
        "status": "processed", "pages_total": 1,
        "pages": [{"page_number": 1, "width": 100, "height": 100, "status": "completed",
                   "regions": [{"index": 1, "label": "other", "task_type": "text",
                                "bbox": [10, 10, 90, 90], "status": "preserved", "content": "",
                                "asset": "flagged.assets/crop.png", "recovery": "Empty native region"}]}],
    }))
    (expected / "clean.layout.json").write_text(json.dumps({
        "status": "processed", "pages_total": 1,
        "pages": [{"page_number": 1, "status": "completed", "regions": []}],
    }))
    exec("".join(notebook["cells"][11]["source"]), namespace)
    assert namespace["OCR_AUDIT_ROOT"] == expected
    assert namespace["OCR_REPAIR_TARGET"] is None
    assert namespace["PARTIAL_DOCUMENTS_ONLY"] is False
    assert namespace["EXECUTE_OCR_REPAIRS"] is True
    assert len(namespace["ocr_quality_reports"]) == 2
    assert [r.sidecar_path.name for r in namespace["ocr_repair_queue"]] == ["flagged.layout.json"]


def native_fixture(tmp_path, count=1):
    source = tmp_path / "source.png"
    Image.new("RGB", (100, 100), "white").save(source)
    sidecar = tmp_path / "native.layout.json"
    payload = {
        "status": "processed", "source": str(source), "source_signature": audit._source_signature(source),
        "settings": {"layout_mode": "native"}, "pages_total": 1,
        "native": {"raw_content": "Original document evidence", "merge_groups": []},
        "pages": [{"page_number": 1, "width": 100, "height": 100, "status": "completed", "regions": [
            {"index": i, "label": "text", "task_type": "text", "bbox": [10, 10, 90, 90],
             "status": "preserved", "content": "", "raw_content": "", "asset": None,
             "recovery": "Native OCR returned no usable text", "error": None}
            for i in range(1, count + 1)
        ]}],
    }
    sidecar.write_text(json.dumps(payload))
    (tmp_path / "native.md").write_text("Original Markdown")
    return source, sidecar, payload


def test_native_missing_crop_is_regenerated_and_context_retry_is_bounded(tmp_path):
    source, sidecar, payload = native_fixture(tmp_path)
    calls = []

    async def recognize(image, proposal, tile, contextual):
        calls.append((image.size, contextual))
        if not contextual:
            raise ocr._NativeLayoutError("Native v2 page contains unassigned text before its first block")
        return audit.OCRSplitFragment("Recovered text", "Recovered text", "stop", {})

    report = audit.audit_layout_ocr_file(sidecar)
    asyncio.run(audit.repair_native_layout_regions(
        report, audit.propose_ocr_repairs([report]), source, recognize,
        lambda layout: layout["pages"][0]["regions"][0]["content"],
    ))
    repaired = json.loads(sidecar.read_text())
    region = repaired["pages"][0]["regions"][0]
    assert len(calls) == 2 and calls[0][1] is False and calls[1][1] is True
    with Image.open(sidecar.parent / region["asset"]) as crop:
        assert crop.size == (80, 80)
    assert repaired["native"] == payload["native"]
    assert region["content"] == "Recovered text"
    assert region["repair_attempts"][0]["contextual_retry"] is True
    assert not audit.audit_layout_ocr_file(sidecar).needs_repair


def test_empty_native_region_remains_reviewable_without_blocking_neighbor(tmp_path):
    source, sidecar, payload = native_fixture(tmp_path, count=2)
    calls = []

    async def recognize(image, proposal, tile, contextual):
        calls.append((proposal.region_index, contextual))
        if proposal.region_index == 1:
            raise ocr._EmptyOCROutputError("Unlimited-OCR output is empty after post-processing")
        return audit.OCRSplitFragment("Neighbor repaired", "Neighbor repaired", "stop", {})

    report = audit.audit_layout_ocr_file(sidecar)
    asyncio.run(audit.repair_native_layout_regions(
        report, audit.propose_ocr_repairs([report]), source, recognize,
        lambda layout: "\n".join(r["content"] for r in layout["pages"][0]["regions"]),
    ))
    regions = json.loads(sidecar.read_text())["pages"][0]["regions"]
    assert calls == [(1, False), (1, True), (2, False)]
    assert regions[0]["status"] == "preserved" and regions[0]["repair_history"]
    assert regions[1]["content"] == "Neighbor repaired"
    proposals = audit.propose_ocr_repairs([audit.audit_layout_ocr_file(sidecar)])
    assert len(proposals) == 1 and proposals[0].action == "manual_review"


def test_native_region_repair_rejects_source_changes_before_writes(tmp_path):
    import pytest
    source, sidecar, _ = native_fixture(tmp_path)
    before = sidecar.read_bytes()
    Image.new("RGB", (100, 100), "black").save(source)

    async def forbidden(*args):
        raise AssertionError("Changed sources must not make OCR requests")

    report = audit.audit_layout_ocr_file(sidecar)
    with pytest.raises(ValueError, match="Source signature changed"):
        asyncio.run(audit.repair_native_layout_regions(
            report, audit.propose_ocr_repairs([report]), source, forbidden, lambda layout: "",
        ))
    assert sidecar.read_bytes() == before


def test_empty_region_uses_native_pdf_text_before_any_ocr_request(tmp_path):
    import pymupdf
    _, sidecar, payload = native_fixture(tmp_path)
    source = tmp_path / "source.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=200, height=200)
        page.insert_text((20, 40), "Native source text")
        pdf.save(source)
    payload.update(source=str(source), source_signature=audit._source_signature(source))
    page = payload["pages"][0]
    page.update(width=200, height=200)
    page["regions"][0]["bbox"] = [10, 10, 190, 70]
    sidecar.write_text(json.dumps(payload))

    async def forbidden(*args):
        raise AssertionError("Available source text must avoid an OCR request")

    report = audit.audit_layout_ocr_file(sidecar)
    asyncio.run(audit.repair_native_layout_regions(
        report, audit.propose_ocr_repairs([report]), source, forbidden,
        lambda layout: layout["pages"][0]["regions"][0]["content"],
    ))
    region = json.loads(sidecar.read_text())["pages"][0]["regions"][0]
    assert region["content"] == "Native source text"
    assert region["native_text_repair"]["strategy"] == "native_pdf_replacement"
    assert region["repair_history"][0]["status"] == "preserved"


def test_infrastructure_error_keeps_neighbor_progress_and_does_not_repeat_request(tmp_path):
    import pytest
    source, sidecar, _ = native_fixture(tmp_path, count=2)
    calls = []

    async def recognize(image, proposal, tile, contextual):
        calls.append((proposal.region_index, contextual))
        if proposal.region_index == 1:
            raise ConnectionError("OCR service unavailable")
        return audit.OCRSplitFragment("Neighbor repaired", "Neighbor repaired", "stop", {})

    report = audit.audit_layout_ocr_file(sidecar)
    with pytest.raises(RuntimeError, match="OCR service unavailable"):
        asyncio.run(audit.repair_native_layout_regions(
            report, audit.propose_ocr_repairs([report]), source, recognize,
            lambda layout: "\n".join(r["content"] for r in layout["pages"][0]["regions"]),
        ))
    assert calls == [(1, False), (2, False)]
    regions = json.loads(sidecar.read_text())["pages"][0]["regions"]
    assert regions[0]["status"] == "preserved"
    assert regions[1]["content"] == "Neighbor repaired"


def test_reviewed_sideways_table_is_oriented_before_row_splitting(tmp_path):
    source, sidecar, payload = native_fixture(tmp_path)
    Image.new("RGB", (100, 200), "white").save(source)
    payload["source_signature"] = audit._source_signature(source)
    payload["pages"][0].update(width=100, height=200)
    region = payload["pages"][0]["regions"][0]
    region.update(bbox=[10, 10, 90, 190], label="table", task_type="table", status="completed",
                  content="<table><tr><td>incomplete", ocr_rotation_degrees=90)
    sidecar.write_text(json.dumps(payload))
    sizes = []

    async def recognize(image, proposal, tile, contextual):
        sizes.append(image.size)
        content = "<table><tr><td>Readable row</td></tr></table>"
        return audit.OCRSplitFragment(content, content, "stop", {})

    report = audit.audit_layout_ocr_file(sidecar)
    asyncio.run(audit.repair_native_layout_regions(
        report, audit.propose_ocr_repairs([report]), source, recognize,
        lambda layout: layout["pages"][0]["regions"][0]["content"],
    ))
    assert sizes == [(180, 80)]
    repaired = json.loads(sidecar.read_text())["pages"][0]["regions"][0]
    assert repaired["split_repair"]["rotation_degrees_clockwise"] == 90
    assert repaired["bbox"] == [10, 10, 90, 190]


def test_failed_tile_cancels_sibling_request_before_returning(tmp_path):
    import pytest
    source, sidecar, payload = native_fixture(tmp_path)
    image = Image.new("RGB", (100, 600), "white")
    image.save(source)
    payload["source_signature"] = audit._source_signature(source)
    payload["pages"][0].update(width=100, height=600)
    region = payload["pages"][0]["regions"][0]
    region.update(bbox=[0, 0, 100, 600], asset="tile.png")
    image.save(tmp_path / "tile.png")
    sidecar.write_text(json.dumps(payload))

    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def recognize(image, proposal, tile):
            if tile.index == 0:
                await started.wait()
                raise ocr._EmptyOCROutputError("empty tile")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        report = audit.audit_layout_ocr_file(sidecar)
        from dataclasses import replace
        proposal = replace(audit.propose_ocr_repairs([report])[0], action="split_and_retry")
        with pytest.raises(ocr._EmptyOCROutputError):
            await audit.split_and_stitch_layout_regions(
                report, (proposal,), recognize, lambda layout: "", tile_height=360, overlap=40,
            )
        assert cancelled.is_set()  # Before asyncio.run's final task cleanup.

    asyncio.run(run())


def test_native_crop_cleaner_preserves_prefix_and_full_table():
    import pytest
    table = "<table><tr><td>Visible row</td></tr></table>"
    raw = "<PAGE>Visible heading\n<BLOCK>table[100,100,900,900]<FORK>" + table
    with pytest.raises(ocr._NativeLayoutError):
        ocr._native_blocks(raw, 1)
    cleaned = audit._clean_native_crop_text(raw)
    assert "Visible heading" in cleaned and table in cleaned
    assert "<PAGE>" not in cleaned and "<BLOCK>" not in cleaned
    assert audit._clean_native_crop_text("<PAGE>" + table) == table
    with pytest.raises(ocr._NativeLayoutError, match="multiple physical pages"):
        audit._clean_native_crop_text("<PAGE>First<PAGE>Second")
    with pytest.raises(ocr._NativeLayoutError, match="incomplete coordinates"):
        audit._clean_native_crop_text("<PAGE><BLOCK>text[945,<td>")
