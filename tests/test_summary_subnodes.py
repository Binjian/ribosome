import asyncio
import json
from pathlib import Path

from ribosome.core.dom.summary import summarize_node_async


def test_summarize_node_async_recurses_into_subnodes():
    payload = {
        "t": "Section",
        "c": [],
        "subnodes": [
            {
                "t": "Section",
                "c": [
                    {"t": "Header", "c": [2, ["nested", [], []], [{"t": "Str", "c": "Nested child text"}]]},
                    {"t": "Content", "c": []},
                ],
                "subnodes": [],
            }
        ],
    }

    result = asyncio.run(
        summarize_node_async(
            json.loads(json.dumps(payload)),
            Path('.'),
            object(),
            "unused",
            Path('doc_ast.json'),
            {"count": 0},
            {"count": 0},
            leaf_min_len=9999,
            min_len=9999,
        )
    )

    child_summary = result["subnodes"][0]["s"]

    assert child_summary
    assert "Nested child text" in child_summary
    assert result["s"] == ""


def test_summarize_nodes_without_content_have_empty_summaries():
    payload = [
        {"t": node_type, **content, **summary}
        for node_type in ("Str", "Section", "Image", "AlignDefault")
        for content in ({}, {"c": []}, {"c": ""}, {"c": None})
        for summary in ({}, {"s": "stale"})
    ]
    result = asyncio.run(
        summarize_node_async(
            payload, Path('.'), object(), "unused", Path('doc_ast.json'),
            {"count": 0}, {"count": 0}, leaf_min_len=9999, min_len=9999,
        )
    )
    assert all(node["s"] == "" for node in result)
