"""Exercise the application configuration and native-crop dispatch offline."""
import ast
import asyncio
import json
from pathlib import Path

from PIL import Image
from ribosome.preprocessing.ocr import audit_repair as audit
from ribosome.preprocessing.ocr import baidu_unlimited as ocr


NOTEBOOK = Path(__file__).resolve().parents[1] / "nbs/03.preprocessing.ocr.audit_repair.ipynb"


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
