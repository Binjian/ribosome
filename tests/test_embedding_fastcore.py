import asyncio
import hashlib
import json
from pathlib import Path

from fastcore.test import test_eq as _test_eq
from fastcore.test import test_fail as _test_fail

from ribosome.core.dom.embedding.natural_text import (
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
        self.rows = {}
        self.operations = []

    @property
    def records(self):
        return list(self.rows.values())

    def upsert(self, ids, metadatas, embeddings, documents):
        self.operations.append(('upsert', list(ids)))
        for i, identifier in enumerate(ids):
            self.rows[identifier] = dict(ids=[identifier], metadatas=[metadatas[i]],
                embeddings=embeddings[i:i+1], documents=[documents[i]])

    def get(self, where=None, include=None):
        self.operations.append(('get', where))
        return {'ids': [identifier for identifier, row in self.rows.items()
            if not where or all(row['metadatas'][0].get(k) == v for k, v in where.items())]}

    def delete(self, ids):
        self.operations.append(('delete', list(ids)))
        for identifier in ids:
            self.rows.pop(identifier, None)



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

    expected_metadata = {
        "embed_model": "embed-model",
        "node_type": "Section",
        "document_id": "root-id",
        "node_path": "$",
    }
    _test_eq(result["s"], "section summary")
    _test_eq(result["m"], expected_metadata)
    _test_eq(len(result["i"]), 64)
    _test_eq(len(collection.records), 1)
    _test_eq(collection.records[0]["documents"], ["section summary"])
    _test_eq(collection.records[0]["metadatas"], [expected_metadata])
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

    object_path = ["ast-id"]
    result = asyncio.run(
        embed_walk_node(
            node=semantics,
            cur_object_path=object_path,
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
    # the compatibility ancestor seed remains unchanged
    _test_eq(object_path, ["ast-id"])
    # structural paths are independent of ancestor IDs
    _test_eq(collection.records[0]["metadatas"][0]["node_path"], "$/children/0")
    _test_eq(collection.records[0]["metadatas"][0]["document_id"], "ast-id")


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
    _test_eq(result["m"]["embed_model"], "ast-model")
    _test_eq(result["m"]["node_type"], "Document")
    _test_eq(result["m"]["document_id"], result["i"])
    _test_eq(result["m"]["node_path"], "$")
    _test_eq(result["m"]["file_path"], "sample.md")
    _test_eq(result["seen"], True)
    _test_eq(result["children"][0]["seen"], True)
    _test_eq(len(result["i"]), 64)
    _test_eq(len(result["children"][0]["i"]), 64)
    _test_eq(len(collection.records), 2)
    _test_eq(collection.records[0]["documents"], ["document summary"])
    _test_eq(collection.records[0]["metadatas"], [result["m"]])
    _test_eq(collection.records[1]["documents"], ["section summary"])
    _test_eq(collection.records[1]["metadatas"][0]["node_type"], "Section")
    _test_eq(collection.records[1]["metadatas"][0]["document_id"], result["i"])
    _test_eq(collection.records[1]["metadatas"][0]["node_path"], "$/children/0")
    _test_eq(collection.records[1]["metadatas"][0]["file_path"], "sample.md")
    _test_eq(client.calls[0]["input"], "document summary")
    _test_eq(client.calls[1]["input"], "section summary")


def test_embed_node_handles_dict_response_model():
    collection = FakeCollection()
    client = FakeAsyncClient([{"embeddings": [[0.5, 1.5]], "model": "dict-model"}])

    result = asyncio.run(
        embed_node(
            node={"t": "Section", "s": "section summary"},
            collection=collection,
            cur_object_path=["root-id"],
            embed_types={"Section"},
            llm_client=client,
            model="seed-model",
        )
    )

    _test_eq(result["m"]["embed_model"], "dict-model")
    _test_eq(len(collection.records), 1)


def test_embed_node_dict_response_without_model_falls_back_to_requested():
    collection = FakeCollection()
    client = FakeAsyncClient([{"embeddings": [[0.5, 1.5]]}])

    result = asyncio.run(
        embed_node(
            node={"t": "Section", "s": "section summary"},
            collection=collection,
            cur_object_path=["root-id"],
            embed_types={"Section"},
            llm_client=client,
            model="seed-model",
        )
    )

    _test_eq(result["m"]["embed_model"], "seed-model")


def test_embed_walk_node_dict_child_embedded_once():
    semantics = {
        "t": "Section",
        "s": "root summary",
        "meta_node": {"t": "Table", "s": "table summary"},
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

    # regression: the dict-valued child must be embedded exactly once (child first, parent last)
    _test_eq(len(collection.records), 2)
    _test_eq(len(client.calls), 2)
    _test_eq(collection.records[0]["documents"], ["table summary"])
    _test_eq(collection.records[1]["documents"], ["root summary"])
    ids = [record["ids"][0] for record in collection.records]
    _test_eq(len(set(ids)), 2)
    # action ran before embedding, and the surviving id is the one stored in the collection
    _test_eq(result["meta_node"]["visited"], True)
    _test_eq(result["meta_node"]["i"], ids[0])


def test_embed_walk_node_ancestor_path_and_determinism():
    def make_ast():
        return {
            "t": "Section",
            "s": "outer summary",
            "children": [
                {
                    "t": "Section",
                    "s": "inner summary",
                    "children": [{"t": "Table", "s": "table summary"}],
                },
            ],
        }

    def walk():
        collection = FakeCollection()
        client = FakeAsyncClient([FakeEmbedResponse([[1, 1]], model="walk-model")] * 3)
        object_path = ["ast-id"]
        result = asyncio.run(
            embed_walk_node(
                node=make_ast(),
                cur_object_path=object_path,
                collection=collection,
                embed_types={"Section", "Table"},
                llm_client=client,
                model="seed-model",
            )
        )
        _test_eq(object_path, ["ast-id"])
        return result, collection

    result, collection = walk()
    outer_i = result["i"]
    inner_i = result["children"][0]["i"]
    table_metadata = collection.records[0]["metadatas"][0]
    _test_eq(collection.records[0]["documents"], ["table summary"])
    # node_path records dict keys and list indexes
    _test_eq(table_metadata["node_path"], "$/children/0/children/0")
    _test_eq(table_metadata["document_id"], "ast-id")
    _test_eq(collection.records[1]["metadatas"][0]["node_path"], "$/children/0")
    _test_eq(collection.records[2]["metadatas"][0]["node_path"], "$")

    # ids are deterministic across walks of identical content
    result2, collection2 = walk()
    _test_eq(result2["i"], outer_i)
    _test_eq([r["ids"] for r in collection2.records], [r["ids"] for r in collection.records])


def test_embed_root_embedded_once_when_root_type_is_embeddable():
    semantics = {"t": "Section", "s": "root summary", "children": []}
    collection = FakeCollection()
    client = FakeAsyncClient([FakeEmbedResponse([[2, 2]], model="root-model")])

    result_json = asyncio.run(
        embed(
            semantics_json=json.dumps(semantics),
            file_path=Path("sample.md"),
            embed_types={"Section"},
            db_client=None,
            collection=collection,
            llm_client=client,
            model="seed-model",
        )
    )

    result = json.loads(result_json)
    canonical_json = json.dumps(
        semantics, sort_keys=True, ensure_ascii=False, separators=(",", ";")
    ).encode("utf-8").decode("utf-8")
    expected_id = hashlib.sha256(("sample.md" + canonical_json).encode("utf-8")).hexdigest()
    # regression: root keeps the id assigned by embed() and is embedded exactly once
    _test_eq(result["i"], expected_id)
    _test_eq(len(collection.records), 1)
    _test_eq(len(client.calls), 1)
    _test_eq(collection.records[0]["ids"], [expected_id])
    _test_eq(collection.records[0]["metadatas"][0]["node_type"], "Section")


def test_embed_skips_empty_root_summary():
    semantics = {"summary": "", "blocks": [{"t": "Section", "s": "section summary", "children": []}]}
    collection = FakeCollection()
    client = FakeAsyncClient([FakeEmbedResponse([[3, 3]], model="section-model")])

    asyncio.run(
        embed(
            semantics_json=json.dumps(semantics),
            file_path=Path("sample.md"),
            embed_types={"Section"},
            db_client=None,
            collection=collection,
            llm_client=client,
            model="seed-model",
        )
    )

    _test_eq(len(client.calls), 1)
    _test_eq(client.calls[0]["input"], "section summary")
    _test_eq(len(collection.records), 1)
    _test_eq(collection.records[0]["documents"], ["section summary"])
