import asyncio
import json
from pathlib import Path

from fastcore.test import test_eq as _test_eq
from fastcore.test import test_fail as _test_fail

from ribosome.core.dom.embedding import (
    embed,
    embed_node,
    embed_walk_node,
    get_embeddings_with_retry,
    normalize_embeddings,
)


class FakeEmbedResponse:
    def __init__(self, embeddings=None, model="fake-model"):
        self.embeddings = embeddings
        self.model = model


class FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def embed(self, model: str, input: str):
        self.calls.append({"model": model, "input": input})
        if not self.responses:
            raise AssertionError("No fake responses remaining")
        response = self.responses.pop(0)
        return response() if callable(response) else response


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


def test_normalize_embeddings_handles_supported_shapes():
    _test_eq(normalize_embeddings(None), None)
    _test_eq(normalize_embeddings([]), None)
    _test_eq(normalize_embeddings([1, 2.5, 3]), [[1.0, 2.5, 3.0]])
    _test_eq(normalize_embeddings([[1, 2], [3.5, 4]]), [[1.0, 2.0], [3.5, 4.0]])
    _test_eq(normalize_embeddings({"embedding": [1, 2]}), [[1.0, 2.0]])
    _test_eq(normalize_embeddings({"embeddings": [[1, 2], [], [3, 4]]}), [[1.0, 2.0], [3.0, 4.0]])
    _test_eq(normalize_embeddings("invalid"), None)


def test_get_embeddings_with_retry_returns_first_valid_payload():
    client = FakeAsyncClient([
        {"embeddings": [[0.1, 0.2, 0.3]]},
    ])

    embeddings, response = asyncio.run(
        get_embeddings_with_retry("hello", "item-1", client=client, model="m1")
    )

    _test_eq(embeddings, [0.1, 0.2, 0.3])
    _test_eq(response["embeddings"], [[0.1, 0.2, 0.3]])
    _test_eq(len(client.calls), 1)


def test_get_embeddings_with_retry_retries_once_then_succeeds():
    client = FakeAsyncClient([
        {"embedding": []},
        FakeEmbedResponse([[1, 2, 3]], model="retry-model"),
    ])

    embeddings, response = asyncio.run(
        get_embeddings_with_retry("hello", "item-2", client=client, model="m2")
    )

    _test_eq(embeddings, [1.0, 2.0, 3.0])
    _test_eq(response.model, "retry-model")
    _test_eq(len(client.calls), 2)


def test_get_embeddings_with_retry_returns_none_after_failed_retry():
    client = FakeAsyncClient([
        {"embeddings": []},
        FakeEmbedResponse(None),
    ])

    embeddings, response = asyncio.run(
        get_embeddings_with_retry("hello", "item-3", client=client, model="m3")
    )

    _test_eq(embeddings, None)
    _test_eq(response.embeddings, None)
    _test_eq(len(client.calls), 2)


def test_embed_node_adds_collection_record_for_supported_node():
    node = {"t": "Section", "s": "section summary"}
    collection = FakeCollection()
    client = FakeAsyncClient([FakeEmbedResponse([[0.5, 1.5]], model="embed-model")])

    result = asyncio.run(
        embed_node(
            node=node,
            collection=collection,
            cur_object_path=["root-id"],
            embed_types={"Section"},
            llm_client=client,
            model="seed-model",
        )
    )

    _test_eq(result["s"], "section summary")
    _test_eq(result["m"], {"embed_model": "embed-model"})
    _test_eq(len(result["i"]), 64)
    _test_eq(len(collection.records), 1)
    _test_eq(collection.records[0]["documents"], ["section summary"])
    _test_eq(collection.records[0]["metadatas"], [{"embed_model": "embed-model"}])
    _test_eq(collection.records[0]["embeddings"].shape, (1, 2))


def test_embed_node_skips_empty_summary_and_rejects_wrong_type():
    collection = FakeCollection()
    client = FakeAsyncClient([])
    empty_node = {"t": "Section", "s": ""}

    result = asyncio.run(
        embed_node(
            node=empty_node,
            collection=collection,
            cur_object_path=["root-id"],
            embed_types={"Section"},
            llm_client=client,
            model="seed-model",
        )
    )

    _test_eq(result.get("m"), None)
    _test_eq(len(collection.records), 0)
    _test_eq(len(client.calls), 0)
    _test_fail(
        lambda: asyncio.run(
            embed_node(
                node={"t": "Para", "s": "nope"},
                collection=collection,
                cur_object_path=["root-id"],
                embed_types={"Section"},
                llm_client=client,
                model="seed-model",
            )
        ),
        contains="Node type Para is not in embed_types",
    )


def test_embed_walk_node_recurses_lists_and_applies_action():
    semantics = {
        "t": "Section",
        "s": "root summary",
        "children": [
            {"t": "Table", "s": "table summary", "rows": []},
            {"plain": "value"},
        ],
    }
    collection = FakeCollection()
    client = FakeAsyncClient([
        FakeEmbedResponse([[1, 0]], model="walk-model"),
        FakeEmbedResponse([[0, 1]], model="walk-model"),
    ])

    result = asyncio.run(
        embed_walk_node(
            node=semantics,
            cur_object_path=["ast-id"],
            collection=collection,
            embed_types={"Section", "Table"},
            llm_client=client,
            model="seed-model",
            action=lambda node: {**node, "visited": True} if isinstance(node, dict) else node,
        )
    )

    _test_eq(result["visited"], True)
    _test_eq(result["children"][0]["visited"], True)
    _test_eq(result["children"][1]["visited"], True)
    _test_eq(len(collection.records), 2)
    _test_eq(collection.records[0]["documents"], ["table summary"])
    _test_eq(collection.records[1]["documents"], ["root summary"])


def test_embed_adds_ast_and_nested_nodes_then_returns_json():
    semantics = {
        "t": "Document",
        "s": "document summary",
        "children": [
            {"t": "Section", "s": "section summary", "children": []}
        ],
    }
    collection = FakeCollection()
    client = FakeAsyncClient([
        FakeEmbedResponse([[9, 9]], model="ast-model"),
        FakeEmbedResponse([[1, 1]], model="section-model"),
    ])

    result_json = asyncio.run(
        embed(
            semantics_json=json.dumps(semantics),
            file_path=Path("sample.md"),
            embed_types={"Section"},
            db_client=None,
            collection=collection,
            llm_client=client,
            model="seed-model",
            action=lambda node: {**node, "seen": True} if isinstance(node, dict) else node,
        )
    )

    result = json.loads(result_json)
    _test_eq(result["m"], {"embed_model": "seed-model"})
    _test_eq(result["seen"], True)
    _test_eq(result["children"][0]["seen"], True)
    _test_eq(len(result["i"]), 64)
    _test_eq(len(result["children"][0]["i"]), 64)
    _test_eq(len(collection.records), 2)
    _test_eq(collection.records[0]["documents"], ["document summary"])
    _test_eq(collection.records[1]["documents"], ["section summary"])
    _test_eq(client.calls[0]["input"], "document summary")
    _test_eq(client.calls[1]["input"], "section summary")
