import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DASHSCOPE_API_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cell_ribosome.dom import DOM


class FakeEmbedResponse:
    def __init__(self, text: str):
        self.embedding = [float(len(text or ""))]
        self.model = "fake-embed-model"


class FakeOllamaClient:
    async def embed(self, model: str, input: str):
        return FakeEmbedResponse(input)


class FakeCollection:
    def __init__(self):
        self.records = []

    def add(self, ids, metadatas, embeddings, documents):
        self.records.append(
            {
                "ids": ids,
                "metadatas": metadatas,
                "embeddings": embeddings,
                "documents": documents,
            }
        )


class FakeDbClient:
    def __init__(self, collection: FakeCollection):
        self.collection = collection

    def get_or_create_collection(self, name: str):
        return self.collection


class DOMBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.md_file = self.root / "doc.md"
        self.md_file.write_text("# title\n", encoding="utf-8")

    def make_dom(self) -> DOM:
        dom = DOM(self.md_file)
        object.__setattr__(dom, "ollama_client", FakeOllamaClient())
        original_min_len = type(dom).leaf_min_len
        self.addCleanup(setattr, type(dom), "leaf_min_len", original_min_len)
        type(dom).leaf_min_len = 10
        return dom

    def test_textualize_summarizes_text_and_images(self):
        dom = self.make_dom()
        ast = {
            "blocks": [
                {
                    "t": "Section",
                    "c": [
                        {"t": "Header", "c": [1, ["intro", [], []], [{"t": "Str", "c": "Intro"}]]},
                        {
                            "t": "Content",
                            "c": [
                                {
                                    "t": "Para",
                                    "c": [
                                        {
                                            "t": "Str",
                                            "c": "This paragraph is intentionally long enough to trigger summarization.",
                                        }
                                    ],
                                },
                                {"t": "Image", "c": [["", [], []], [], ["robot.png", ""]]},
                            ],
                        },
                    ],
                }
            ]
        }
        dom.ast_json = json.dumps(ast, ensure_ascii=False)
        dom.ast_json_file = self.root / "doc_ast.json"
        dom.ast_json_file.write_text(dom.ast_json, encoding="utf-8")
        (self.root / "robot.png").write_bytes(b"fake image")

        async def fake_text_summary(client, content, model="unused", role="user", lang="zh"):
            return f"TXT<{content[:24]}>"

        async def fake_image_summary(client, image_link, model="unused", role="user", lang="zh"):
            return f"IMG<{Path(image_link).name}>"

        with patch("cell_ribosome.dom.get_summary_response_async", side_effect=fake_text_summary), patch(
            "cell_ribosome.dom.get_image_summary_async", side_effect=fake_image_summary
        ):
            asyncio.run(dom.textualize())

        result = json.loads(dom.ast_json)
        section = result["blocks"][0]
        content_nodes = section["c"][1]["c"]
        para = content_nodes[0]
        image = content_nodes[1]

        self.assertTrue(para["s"].startswith("TXT<"))
        self.assertEqual(image["s"], "IMG<robot.png>")
        self.assertEqual(result["title"], "doc")
        self.assertEqual(result["file_path"], str(self.md_file))
        self.assertIn("summary", result)
        self.assertTrue(result["summary"])

    def test_textualize_wraps_unexpected_errors_with_context(self):
        dom = self.make_dom()
        ast = {
            "blocks": [
                {
                    "t": "Section",
                    "c": [
                        {"t": "Header", "c": [1, ["intro", [], []], [{"t": "Str", "c": "Intro"}]]},
                        {"t": "Content", "c": [{"t": "Para", "c": [{"t": "Str", "c": "Long enough text for failure"}]}]},
                    ],
                }
            ]
        }
        dom.ast_json = json.dumps(ast, ensure_ascii=False)
        dom.ast_json_file = self.root / "doc_ast.json"
        dom.ast_json_file.write_text(dom.ast_json, encoding="utf-8")

        async def boom(*args, **kwargs):
            raise RuntimeError("backend exploded")

        with patch("cell_ribosome.dom.get_summary_response_async", side_effect=boom):
            with self.assertRaises(ValueError) as ctx:
                asyncio.run(dom.textualize())

        self.assertIn("Error summarizing node", str(ctx.exception))
        self.assertIn("backend exploded", str(ctx.exception))

    def test_embed_uses_parent_stack_for_sibling_and_nested_nodes(self):
        dom = self.make_dom()
        semantics = {
            "summary": "document summary",
            "blocks": [
                {
                    "t": "Section",
                    "s": "section one",
                    "c": [
                        {"t": "Header", "c": [1, ["s1", [], []], [{"t": "Str", "c": "S1"}]]},
                        {"t": "Content", "c": [{"t": "Table", "s": "table one", "c": []}]},
                    ],
                },
                {
                    "t": "Section",
                    "s": "section two",
                    "c": [
                        {"t": "Header", "c": [1, ["s2", [], []], [{"t": "Str", "c": "S2"}]]},
                        {"t": "Content", "c": []},
                    ],
                },
            ],
        }
        dom.semantics_json = json.dumps(semantics, ensure_ascii=False)

        collection = FakeCollection()

        with patch("cell_ribosome.dom.PersistentClient", side_effect=lambda *args, **kwargs: SimpleNamespace()), patch(
            "cell_ribosome.dom.chromadb.EphemeralClient", side_effect=lambda: FakeDbClient(collection)
        ):
            asyncio.run(dom.embed(db_path=self.root / "db"))

        self.assertEqual(len(collection.records), 4)
        root_path = collection.records[0]["metadatas"][0]["obj_path"]
        section_one_path = collection.records[1]["metadatas"][0]["obj_path"]
        table_path = collection.records[2]["metadatas"][0]["obj_path"]
        section_two_path = collection.records[3]["metadatas"][0]["obj_path"]

        self.assertEqual(len(root_path), 1)
        self.assertEqual(section_one_path, root_path)
        self.assertEqual(len(table_path), 2)
        self.assertEqual(table_path[0], root_path[0])
        self.assertEqual(section_two_path, root_path)


if __name__ == "__main__":
    unittest.main()
