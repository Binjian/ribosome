import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from fastcore.test import *


def _register_module(name: str) -> ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        sys.modules[name] = module
    return module


def _install_dependency_stubs() -> None:
    ipython = _register_module("IPython")
    ipython_core = _register_module("IPython.core")
    interactiveshell = _register_module("IPython.core.interactiveshell")
    ipython_display = _register_module("IPython.display")

    class _Shell:
        ast_node_interactivity = "all"

        @staticmethod
        def instance():
            return _Shell()

    interactiveshell.InteractiveShell = _Shell
    ipython_display.Markdown = str
    ipython_display.display = lambda *args, **kwargs: None
    ipython.core = ipython_core
    ipython_core.interactiveshell = interactiveshell

    nbdev = _register_module("nbdev")
    nbdev_showdoc = _register_module("nbdev.showdoc")
    nbdev.showdoc = nbdev_showdoc

    pydantic = _register_module("pydantic")

    class _BaseModel:
        pass

    pydantic.BaseModel = _BaseModel
    pydantic.Field = lambda *args, **kwargs: None

    ollama = _register_module("ollama")

    class _AsyncClient:
        pass

    ollama.AsyncClient = _AsyncClient

    openai = _register_module("openai")
    openai.AsyncOpenAI = object

    chromadb = _register_module("chromadb")
    chromadb_api = _register_module("chromadb.api")
    chromadb_api_models = _register_module("chromadb.api.models")
    chromadb_api_models_collection = _register_module("chromadb.api.models.Collection")
    chromadb_config = _register_module("chromadb.config")

    class _ClientAPI:
        pass

    class _Collection:
        pass

    class _Settings:
        pass

    chromadb_api.ClientAPI = _ClientAPI
    chromadb_api_models_collection.Collection = _Collection
    chromadb_config.Settings = _Settings
    chromadb.PersistentClient = object
    chromadb.api = chromadb_api
    chromadb.config = chromadb_config

    jsoncfg = _register_module("jsoncfg")
    jsoncfg_config_classes = _register_module("jsoncfg.config_classes")

    class _ConfigNode:
        pass

    jsoncfg_config_classes.ConfigJSONArray = _ConfigNode
    jsoncfg_config_classes.ConfigJSONObject = _ConfigNode
    jsoncfg_config_classes.ConfigJSONScalar = _ConfigNode
    jsoncfg_config_classes.ConfigNode = _ConfigNode
    jsoncfg.config_classes = jsoncfg_config_classes

    dotenv = _register_module("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_install_dependency_stubs()

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
    test_eq(normalize_embeddings(None), None)
    test_eq(normalize_embeddings([]), None)
    test_eq(normalize_embeddings([1, 2.5, 3]), [[1.0, 2.5, 3.0]])
    test_eq(normalize_embeddings([[1, 2], [3.5, 4]]), [[1.0, 2.0], [3.5, 4.0]])
    test_eq(normalize_embeddings({"embedding": [1, 2]}), [[1.0, 2.0]])
    test_eq(normalize_embeddings({"embeddings": [[1, 2], [], [3, 4]]}), [[1.0, 2.0], [3.0, 4.0]])
    test_eq(normalize_embeddings("invalid"), None)


def test_get_embeddings_with_retry_returns_first_valid_payload():
    client = FakeAsyncClient([
        {"embeddings": [[0.1, 0.2, 0.3]]},
    ])

    embeddings, response = asyncio.run(
        get_embeddings_with_retry("hello", "item-1", client=client, model="m1")
    )

    test_eq(embeddings, [0.1, 0.2, 0.3])
    test_eq(response["embeddings"], [[0.1, 0.2, 0.3]])
    test_eq(len(client.calls), 1)


def test_get_embeddings_with_retry_retries_once_then_succeeds():
    client = FakeAsyncClient([
        {"embedding": []},
        FakeEmbedResponse([[1, 2, 3]], model="retry-model"),
    ])

    embeddings, response = asyncio.run(
        get_embeddings_with_retry("hello", "item-2", client=client, model="m2")
    )

    test_eq(embeddings, [1.0, 2.0, 3.0])
    test_eq(response.model, "retry-model")
    test_eq(len(client.calls), 2)


def test_get_embeddings_with_retry_returns_none_after_failed_retry():
    client = FakeAsyncClient([
        {"embeddings": []},
        FakeEmbedResponse(None),
    ])

    embeddings, response = asyncio.run(
        get_embeddings_with_retry("hello", "item-3", client=client, model="m3")
    )

    test_eq(embeddings, None)
    test_eq(response.embeddings, None)
    test_eq(len(client.calls), 2)


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

    test_eq(result["s"], "section summary")
    test_eq(result["m"], {"embed_model": "embed-model"})
    test_eq(len(result["i"]), 64)
    test_eq(len(collection.records), 1)
    test_eq(collection.records[0]["documents"], ["section summary"])
    test_eq(collection.records[0]["metadatas"], [{"embed_model": "embed-model"}])
    test_eq(collection.records[0]["embeddings"].shape, (1, 2))


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

    test_eq(result.get("m"), None)
    test_eq(len(collection.records), 0)
    test_eq(len(client.calls), 0)
    test_fail(
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

    test_eq(result["visited"], True)
    test_eq(result["children"][0]["visited"], True)
    test_eq(result["children"][1]["visited"], True)
    test_eq(len(collection.records), 2)
    test_eq(collection.records[0]["documents"], ["table summary"])
    test_eq(collection.records[1]["documents"], ["root summary"])


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
    test_eq(result["m"], {"embed_model": "seed-model"})
    test_eq(result["seen"], True)
    test_eq(result["children"][0]["seen"], True)
    test_eq(len(result["i"]), 64)
    test_eq(len(result["children"][0]["i"]), 64)
    test_eq(len(collection.records), 2)
    test_eq(collection.records[0]["documents"], ["document summary"])
    test_eq(collection.records[1]["documents"], ["section summary"])
    test_eq(client.calls[0]["input"], "document summary")
    test_eq(client.calls[1]["input"], "section summary")