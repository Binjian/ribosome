import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chromadb

os.environ.setdefault("DASHSCOPE_API_KEY", "test-key")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ribosome.core.dom.model import DOMClass as DOM


class FakeEmbedResponse:
    def __init__(self, text: str):
        self.embeddings = [[float(len(text or ""))]]
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
        dom = DOM(self.md_file, db_client=chromadb.EphemeralClient())
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

        with patch("ribosome.core.dom.summary.get_summary_response_async", side_effect=fake_text_summary), patch(
            "ribosome.core.dom.summary.get_image_summary_async", side_effect=fake_image_summary
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

        with patch("ribosome.core.dom.summary.get_summary_response_async", side_effect=boom):
            with self.assertRaises(ValueError) as ctx:
                asyncio.run(dom.textualize())

        self.assertIn("Error summarizing node", str(ctx.exception))
        self.assertIn("backend exploded", str(ctx.exception))

    def test_embed_doc_embeds_document_and_supported_nodes(self):
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

        with patch(
            "ribosome.core.dom.model.PersistentClient",
            return_value=FakeDbClient(collection),
        ):
            asyncio.run(dom.embed_doc(db_path=self.root / "db"))

        self.assertEqual(len(collection.records), 4)
        self.assertEqual(
            collection.records[0]["metadatas"],
            [{"embed_model": dom.ollama_model}],
        )
        self.assertEqual(
            [record["documents"][0] for record in collection.records],
            ["document summary", "table one", "section one", "section two"],
        )
        self.assertEqual(
            len({record["ids"][0] for record in collection.records}),
            4,
        )


if __name__ == "__main__":
    unittest.main()
