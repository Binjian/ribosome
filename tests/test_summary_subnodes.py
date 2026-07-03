import asyncio
import json
from pathlib import Path

import jsoncfg

from ribosome.core.dom.summary import summary_node_pair_async


def test_summary_node_pair_recurses_into_subnodes():
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
    config = jsoncfg.loads_config(json.dumps(payload, ensure_ascii=False))

    _, result = asyncio.run(
        summary_node_pair_async(
            config,
            json.loads(json.dumps(payload)),
            Path('.'),
            object(),
            "unused",
            Path('doc_ast.json'),
            0,
            0,
            leaf_min_len=9999,
            min_len=9999,
        )
    )

    child_summary = result["subnodes"][0]["s"]

    assert child_summary
    assert "Nested child text" in child_summary
    assert result["s"] == child_summary
