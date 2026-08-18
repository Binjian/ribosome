import json
from pathlib import Path

import fitz


def build_synthetic_bundle(root: Path, *, with_pdf: bool = True) -> dict[str, Path]:
    """Build a small schema-v2 bundle with a cross-page table and bad OCR."""
    root.mkdir(parents=True, exist_ok=True)
    stem = "SX000001-layout-test(A-1)"
    layout_path = root / f"{stem}.layout.json"
    markdown_path = root / f"{stem}.md"
    pdf_path = root / f"{stem}.pdf"
    assets_path = root / f"{stem}.assets"
    assets_path.mkdir()
    table_asset = assets_path / "page-0002-region-001-table.png"
    figure_asset = assets_path / "page-0002-region-002-image.png"
    table_asset.write_bytes(b"table crop")
    figure_asset.write_bytes(b"figure crop")

    if with_pdf:
        with fitz.open() as pdf:
            page1 = pdf.new_page(width=600, height=800)
            page1.insert_text((55, 82), "4. 4. 4 MOVS")
            page1.insert_text((55, 145), "Motion command exact syntax OUT_T IO")
            page2 = pdf.new_page(width=600, height=800)
            page2.insert_text((55, 82), "MOVS P1 OUT_T IO")
            page2.insert_text((55, 315), "4.4.5 JUMP")
            page2.insert_text((55, 375), "WAIT IO T=5")
            pdf.save(pdf_path)

    table_html = (
        '<table><tr><td rowspan="2">MOVS</td><td>格式</td>'
        '<td>MOVS P&lt;参数 1&gt; OUT_T IO</td></tr>'
        '<tr><td colspan="2">运动指令</td></tr></table>'
    )
    suspicious = (
        "The Ground Truth image is clean. According to Rule 2, this is valid. "
        "Provided OCR content follows but is evaluator leakage."
    )
    pages = [
        {
            "page_number": 1,
            "width": 600,
            "height": 800,
            "status": "completed",
            "error": None,
            "regions": [
                _region(1, "paragraph_title", [45, 55, 310, 100], "text", "4. 4. 4 MOVS"),
                _region(2, "text", [45, 115, 500, 165], "text", "Motion command exact syntax OUT_T IO"),
                _region(3, "footer", [40, 740, 200, 775], "text", "SX000001-A/1"),
            ],
        },
        {
            "page_number": 2,
            "width": 600,
            "height": 800,
            "status": "completed",
            "error": None,
            "regions": [
                _region(
                    1,
                    "table",
                    [45, 50, 520, 230],
                    "table",
                    table_html,
                    asset=f"{stem}.assets/{table_asset.name}",
                ),
                _region(
                    2,
                    "image",
                    [100, 100, 200, 180],
                    "figure",
                    "",
                    status="preserved",
                    asset=f"{stem}.assets/{figure_asset.name}",
                ),
                _region(3, "figure_title", [90, 235, 310, 270], "text", "Figure inside table"),
                _region(4, "paragraph_title", [45, 285, 310, 330], "text", "4.4.5 JUMP"),
                _region(5, "table", [45, 340, 500, 420], "table", suspicious),
                _region(6, "footer", [40, 740, 200, 775], "text", "SX000001-A/1"),
            ],
        },
    ]
    source_path = root / "obsolete" / f"{stem}.pdf"
    payload = {
        "schema_version": 2,
        "status": "completed",
        "source": str(source_path),
        "source_signature": {
            "path": str(source_path),
            "size": pdf_path.stat().st_size if with_pdf else 0,
            "mtime_ns": 1,
        },
        "settings": {
            "provider": "synthetic",
            "model": "synthetic-ocr",
            "layout_model": "synthetic-layout",
            "dpi": 72,
        },
        "provider": "synthetic",
        "model": "synthetic-ocr",
        "layout_model": "synthetic-layout",
        "dpi": 72,
        "pages_total": 2,
        "pages": pages,
    }
    layout_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(pages), encoding="utf-8")
    result = {
        "layout": layout_path,
        "markdown": markdown_path,
        "assets": assets_path,
    }
    if with_pdf:
        result["pdf"] = pdf_path
    return result


def _region(
    index: int,
    label: str,
    bbox: list[int],
    task_type: str,
    content: str,
    *,
    status: str = "completed",
    asset: str | None = None,
) -> dict:
    return {
        "index": index,
        "label": label,
        "score": 0.9,
        "bbox": bbox,
        "task_type": task_type,
        "status": status,
        "prompt": None if task_type == "figure" else "<image>document parsing.",
        "max_tokens": None if task_type == "figure" else 2048,
        "asset": asset,
        "raw_content": content,
        "content": content,
        "error": None,
        "recovery": None,
        **(
            {}
            if status == "preserved"
            else {
                "input_mime_type": "image/png",
                "elapsed_s": 0.1,
                "response_id": f"response-{index}",
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ),
    }


def _markdown(pages: list[dict]) -> str:
    parts: list[str] = []
    for page in pages:
        parts.append(f"<!-- Page {page['page_number']} -->")
        for region in page["regions"]:
            bbox = ",".join(str(value) for value in region["bbox"])
            parts.append(
                f"<!-- layout-region page={page['page_number']} index={region['index']} "
                f"label={region['label']} bbox={bbox} status={region['status']} -->"
            )
            if region["asset"]:
                parts.append(f"![asset](<{region['asset']}>)")
            if region["content"]:
                heading = "## " if region["label"] == "paragraph_title" else ""
                parts.append(heading + region["content"])
    return "\n\n".join(parts) + "\n"
