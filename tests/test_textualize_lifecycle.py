"""Offline acceptance coverage for the completed textualize Phases 1–4."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import chromadb
import pytest

from ribosome.core.dom.model import DOMClass


@pytest.fixture
def dom(tmp_path, monkeypatch):
    source = tmp_path / 'doc.md'
    source.write_text('# 文档\n', encoding='utf-8')
    instance = DOMClass(source, db_client=chromadb.EphemeralClient())
    monkeypatch.setattr(DOMClass, 'leaf_min_len', 1)
    monkeypatch.setattr(DOMClass, 'min_len', 1)
    instance.ast_json = json.dumps({'blocks': [{'t': 'Section', 'c': [
        {'t': 'Str', 'c': '内容'}]}]}, ensure_ascii=False)
    instance.ast_json_file = tmp_path / 'doc_ast.json'
    instance.ast_json_file.write_text(instance.ast_json, encoding='utf-8')
    instance.semantics_json_file = tmp_path / 'doc_semantics.json'
    return instance


async def deterministic_summary(client, text, **kwargs):
    return f'S<{text}>'


def test_textualize_forwards_settings_and_shared_counters(dom, monkeypatch):
    monkeypatch.setattr(DOMClass, 'leaf_min_len', 137)
    monkeypatch.setattr(DOMClass, 'min_len', 263)
    monkeypatch.setattr(DOMClass, 'lang', 'en')
    dom.table_count, dom.section_count = 2, 3
    action = lambda value: value
    captured = {}

    async def worker(**kwargs):
        captured.update(kwargs)
        kwargs['table_count']['count'] += 4
        kwargs['section_count']['count'] += 5
        return [{'t': 'Section', 's': 'summarized'}]

    with patch('ribosome.core.dom.model.summarize_node_async', side_effect=worker):
        asyncio.run(dom.textualize(action=action))
    assert captured['action'] is action
    assert (captured['leaf_min_len'], captured['min_len'], captured['lang']) == (137, 263, 'en')
    assert captured['path'] == '$.blocks'
    assert captured['table_count'] == {'count': dom.table_count} == {'count': 6}
    assert captured['section_count'] == {'count': dom.section_count} == {'count': 8}
    assert captured['table_count'] is not captured['section_count']


def test_textualize_rerun_preserves_artifacts_and_current_counters(dom):
    original = dom.ast_json
    original_bytes = dom.ast_json_file.read_bytes()
    with patch('ribosome.core.dom.summary.get_summary_response_async',
               side_effect=deterministic_summary):
        asyncio.run(dom.textualize())
        first = dom.semantics_json
        assert dom.section_count == 1
        assert dom.semantics_json_file.read_bytes() == first.encode('utf-8')
        asyncio.run(dom.textualize())
    assert dom.semantics_json == first
    assert dom.semantics_json_file.read_bytes() == first.encode('utf-8')
    assert dom.ast_json == original
    assert dom.ast_json_file.read_bytes() == original_bytes
    # Phases 1–4 retain cumulative progress; per-run counts are separate §7.3 work.
    assert dom.section_count == 2
    assert dom.table_count == 0


def test_textualize_empty_blocks_writes_empty_summary_without_request(dom):
    dom.ast_json = '{"blocks": []}'
    request = AsyncMock(side_effect=AssertionError('empty input must not call provider'))
    with patch('ribosome.core.dom.summary.get_summary_response_async', request):
        asyncio.run(dom.textualize())
    request.assert_not_awaited()
    assert json.loads(dom.semantics_json)['summary'] == ''
    assert dom.semantics_json_file.read_bytes() == dom.semantics_json.encode('utf-8')
    assert dom.ast_json == '{"blocks": []}'


def test_textualize_then_embed_doc_uses_new_semantics(dom, tmp_path):
    collection = chromadb.EphemeralClient().get_or_create_collection('phase-four')
    client = AsyncMock()
    client.embed.return_value = SimpleNamespace(embeddings=[[0.5, 1.0]], model='offline-model')
    object.__setattr__(dom, 'ollama_client', client)
    database = chromadb.EphemeralClient()
    with patch('ribosome.core.dom.summary.get_summary_response_async',
               side_effect=deterministic_summary):
        asyncio.run(dom.textualize())
    semantics = json.loads(dom.semantics_json)
    assert dom.embed_json is None
    with patch('ribosome.core.dom.model.PersistentClient', return_value=database), \
         patch.object(database, 'get_or_create_collection', return_value=collection):
        asyncio.run(dom.embed_doc(db_path=tmp_path / 'db'))
    requested = [call.kwargs['input'] for call in client.embed.await_args_list]
    assert requested == [semantics['summary'], semantics['blocks'][0]['s']]
    assert collection.count() == 2
    assert set(collection.get()['documents']) == set(requested)
    assert json.loads(dom.embed_json)['summary'] == semantics['summary']
    assert (tmp_path / 'doc_embed.json').read_bytes() == dom.embed_json.encode('utf-8')
