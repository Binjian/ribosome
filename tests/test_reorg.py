import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ribosome.core.dom.reorg import reorg_node, reorg_section


def header(level, text):
    return {"t": "Header", "c": [level, [text, [], []], [{"t": "Str", "c": text}]]}


def section(level, text):
    return {"t": "Section", "c": [header(level, text), {"t": "Content", "c": []}], "subnodes": []}


def para(text):
    return {"t": "Para", "c": [{"t": "Str", "c": text}]}


def section_title(node):
    return node["c"][0]["c"][2][0]["c"]


def test_reorg_section_moves_subsections_to_subnodes_and_keeps_body_content_separate():
    root = section(1, "Root")
    items = iter([
        para("root body"),
        section(2, "Child one"),
        para("child body"),
        section(2, "Child two"),
        para("child two body"),
        section(1, "Sibling"),
    ])

    packed, remaining = reorg_section(root, items)

    assert [node["t"] for node in packed["c"][1]["c"]] == ["Para"]
    assert [section_title(node) for node in packed["subnodes"]] == ["Child one", "Child two"]
    assert [node["t"] for node in packed["subnodes"][0]["c"][1]["c"]] == ["Para"]
    assert section_title(list(remaining)[0]) == "Sibling"


def test_reorg_node_builds_layered_sections_with_sibling_subnode_lists():
    ast = {
        "blocks": [
            header(1, "A"),
            para("a"),
            header(2, "B"),
            para("b"),
            header(3, "C"),
            para("c"),
            header(2, "D"),
            para("d"),
            header(1, "E"),
            para("e"),
        ]
    }

    result = reorg_node(ast, section_level=[])

    assert [section_title(node) for node in result["blocks"]] == ["A", "E"]
    assert [node["t"] for node in result["blocks"][0]["c"][1]["c"]] == ["Para"]
    assert [section_title(node) for node in result["blocks"][0]["subnodes"]] == ["B", "D"]
    assert [section_title(node) for node in result["blocks"][0]["subnodes"][0]["subnodes"]] == ["C"]
    assert [node["t"] for node in result["blocks"][0]["subnodes"][0]["c"][1]["c"]] == ["Para"]


def ordered_heading(text):
    return {
        "t": "OrderedList",
        "c": [[1, {"t": "Decimal"}, {"t": "Period"}], [[{"t": "Plain", "c": [{"t": "Str", "c": text}]}]]],
    }


def test_reorg_node_drops_toc_entries_and_promotes_leading_ordered_heading():
    ast = {
        "blocks": [
            para("目录"),
            para("1 紧急安全信息概述 1"),
            para("2 紧急安全系统 2"),
            para("2.1 停止系统 2"),
            ordered_heading("紧急安全信息概述"),
            para("chapter one body"),
            header(1, "紧急安全系统"),
            para("chapter two body"),
        ]
    }

    result = reorg_node(ast, section_level=[])

    assert [node["t"] for node in result["blocks"]] == ["Section", "Section"]
    assert [section_title(node) for node in result["blocks"]] == ["紧急安全信息概述", "紧急安全系统"]
    assert [node["t"] for node in result["blocks"][0]["c"][1]["c"]] == ["Para"]
    assert result["blocks"][0]["subnodes"] == []


def test_reorg_node_keeps_body_ordered_lists_as_content():
    ast = {
        "blocks": [
            header(1, "Intro"),
            ordered_heading("not a heading inside a section"),
            header(1, "Next"),
        ]
    }

    result = reorg_node(ast, section_level=[])

    assert [section_title(node) for node in result["blocks"]] == ["Intro", "Next"]
    assert [node["t"] for node in result["blocks"][0]["c"][1]["c"]] == ["OrderedList"]
