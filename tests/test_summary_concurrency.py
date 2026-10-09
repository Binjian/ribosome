"""Deterministic concurrency, cleanup and single-tree acceptance tests."""

import asyncio
import copy
import json
import os
import subprocess
import sys
from time import perf_counter
from pathlib import Path
from types import SimpleNamespace
from weakref import WeakKeyDictionary

import pytest

from ribosome.core.dom import summary as sut


@pytest.fixture(autouse=True)
def budgets(monkeypatch):
    monkeypatch.setattr(sut, '_SUMMARY_CONCURRENCY', 2)
    monkeypatch.setattr(sut, '_SUMMARY_TASK_LIMIT', 3)
    monkeypatch.setattr(sut, '_summary_semaphores', WeakKeyDictionary())


def walk(node, **kwargs):
    settings = dict(root_path=Path('.'), client=object(), model='offline',
                    file_path=Path('doc.md'), table_count={'count': 0},
                    section_count={'count': 0}, leaf_min_len=9999, min_len=9999)
    settings.update(kwargs)
    return sut.summarize_node_async(node, **settings)


@pytest.mark.parametrize('value', ['bad', '0', '-1', '', '1.5', None, True])
def test_invalid_configuration_is_explicit(value):
    with pytest.raises(ValueError, match='positive integer'):
        sut._positive_setting(value, 'RIBOSOME_SUMMARY_CONCURRENCY')


@pytest.mark.parametrize('value,expected', [('8', 8), ('1', 1), ('2', 2), (3, 3)])
def test_positive_configuration(value, expected):
    assert sut._positive_setting(value, 'RIBOSOME_SUMMARY_CONCURRENCY') == expected


@pytest.mark.parametrize('value', [None, 'bad', '0', '-1', '1', '4'])
@pytest.mark.parametrize('setting', ['CONCURRENCY', 'TASK_LIMIT'])
def test_configuration_isolated_at_import(value, setting):
    env = dict(os.environ)
    env.pop('RIBOSOME_SUMMARY_TASK_LIMIT', None)
    env.pop('RIBOSOME_SUMMARY_CONCURRENCY', None)
    name = 'RIBOSOME_SUMMARY_' + setting
    if value is None:
        env.pop(name, None)
    else:
        env[name] = value
    result = subprocess.run(
        [sys.executable, '-c', f'from ribosome.core.dom.summary import _SUMMARY_{setting}; print(_SUMMARY_{setting})'],
        env=env, capture_output=True, text=True, timeout=15)
    if value in {'bad', '0', '-1'}:
        assert result.returncode != 0
        assert name + ' must be a positive integer' in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == (value or '8')


@pytest.mark.parametrize('limit', [1, 2])
def test_text_and_images_share_per_loop_request_budget(monkeypatch, limit):
    monkeypatch.setattr(sut, '_SUMMARY_CONCURRENCY', limit)

    async def scenario():
        release = asyncio.Event()
        saturated = asyncio.Event()
        inflight = peak = 0
        entered = []

        async def response(label):
            nonlocal inflight, peak
            entered.append(label)
            inflight += 1
            peak = max(peak, inflight)
            if inflight == limit:
                saturated.set()
            try:
                await release.wait()
                return label
            finally:
                inflight -= 1

        async def text(client, text, **kwargs):
            return await response('text')

        async def image(**kwargs):
            return await response('image')

        async def legacy_response(**kwargs):
            content = await response('legacy image/text')
            return SimpleNamespace(message=SimpleNamespace(content=content))

        monkeypatch.setattr(sut, 'get_summary_response_async', text)
        monkeypatch.setattr(sut, 'get_image_summary_async', image)
        monkeypatch.setattr(sut, 'get_image_summary_response_async', legacy_response)
        monkeypatch.setattr(sut, 'get_text_summary_response_async', legacy_response)
        nodes = [
            {'t': 'Image', 'c': [[], [], ['offline.png', '']]},
            {'t': 'Str', 'c': 'long text'},
        ]
        # Separate roots share one request semaphore, not task-budget objects.
        documents = [asyncio.create_task(walk(copy.deepcopy(nodes), leaf_min_len=1, min_len=1))
                     for _ in range(2)]
        documents.append(asyncio.create_task(sut.get_leaf_summary_async('legacy', 1, object(), 'm')))
        documents.append(asyncio.create_task(sut.get_list_summary_async(['legacy'], 1, object(), 'm')))
        documents.append(asyncio.create_task(sut.summary_node_async(
            copy.deepcopy(nodes[0]), Path('.'), Path('doc.md'), sut.AsyncClient(), 'm', 0, 0)))
        try:
            await asyncio.wait_for(saturated.wait(), 2)
            assert inflight == limit
            release.set()
            await asyncio.wait_for(asyncio.gather(*documents), 2)
            assert set(entered) == {'text', 'image', 'legacy image/text'}
            assert peak == limit
            assert inflight == 0
        finally:
            release.set()
            for task in documents:
                task.cancel()
            await asyncio.gather(*documents, return_exceptions=True)

    # Force contention again on a fresh event loop; no cross-loop binding errors.
    asyncio.run(scenario())
    asyncio.run(scenario())


def test_bounded_map_preserves_order_on_out_of_order_completion():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = []

        async def item(index, value):
            if index == 0:
                await release.wait()
            else:
                finished.append(index)
                entered.set()
            return value

        task = asyncio.create_task(sut._ordered_summary_map(item, ['a', 'b', 'c']))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert finished and 0 not in finished
            release.set()
            assert await asyncio.wait_for(task, 2) == ['a', 'b', 'c']
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize('depth,width', [(1, 1000), (15, 3)])
@pytest.mark.parametrize('task_limit', [1, 3])
def test_total_spawned_tasks_are_bounded_across_nested_tree(monkeypatch, depth, width, task_limit):
    monkeypatch.setattr(sut, '_SUMMARY_TASK_LIMIT', task_limit)
    async def scenario():
        loop = asyncio.get_running_loop()
        existing_factory = loop.get_task_factory()
        active = set()
        peak = 0
        def factory(loop, coro, **kwargs):
            nonlocal peak
            task = asyncio.Task(coro, loop=loop, **kwargs)
            active.add(task)
            peak = max(peak, len(active))
            task.add_done_callback(active.discard)
            return task

        node = {'t': 'Str', 'c': 'leaf'}
        for _ in range(depth):
            # Broad at the bottom, linear depth above: no exponential fixture.
            node = {'t': 'Section', 'c': [copy.deepcopy(node) for __ in range(width if _ == 0 else 1)]}
        loop.set_task_factory(factory)
        try:
            result = await walk(node)
            assert result['s']
            assert peak <= sut._SUMMARY_TASK_LIMIT
            assert all(task.done() for task in active)
        finally:
            loop.set_task_factory(existing_factory)
    asyncio.run(scenario())


@pytest.mark.parametrize('site', ['dict_children', 'list_items', 'aggregate', 'nested'])
@pytest.mark.parametrize('cancel', [False, True])
def test_failure_and_cancellation_await_all_owned_siblings(monkeypatch, site, cancel):
    async def scenario():
        blocked = asyncio.Event()
        fail = asyncio.Event()
        cleaned = asyncio.Event()
        progress = []
        late_mutations = []

        async def provider(client, text, **kwargs):
            if text == 'blocked':
                blocked.set()
                try:
                    await asyncio.Event().wait()
                    late_mutations.append(text)
                finally:
                    cleaned.set()
            if text == 'failure':
                await fail.wait()
                raise RuntimeError('backend exploded')
            return text

        monkeypatch.setattr(sut, 'get_summary_response_async', provider)
        monkeypatch.setattr(sut, '_record_summary_progress', lambda *args, **kwargs: progress.append(args))
        values = [{'t': 'Str', 'c': text} for text in ['failure', 'blocked']]
        if site == 'dict_children':
            operation = walk({'t': 'Section', 'c': values}, leaf_min_len=1, min_len=1)
        elif site == 'list_items':
            operation = walk(values, leaf_min_len=1, min_len=1)
        elif site == 'nested':
            operation = walk([{'t': 'Section', 'c': values}], leaf_min_len=1, min_len=1)
        else:
            operation = sut._summarize_list_async(['failure', 'blocked'], 1, 1, object(), 'm', 'en')
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            else:
                fail.set()
                with pytest.raises((ValueError, RuntimeError), match='backend exploded'):
                    await asyncio.wait_for(task, 2)
            assert cleaned.is_set()
            snapshot = list(progress)
            await asyncio.sleep(0)  # allow any incorrectly detached task to resume
            assert late_mutations == []
            assert progress == snapshot
            # All permits were released; a later traversal succeeds on this loop.
            monkeypatch.setattr(sut, 'get_summary_response_async',
                                lambda *args, **kwargs: asyncio.sleep(0, result='ok'))
            assert (await asyncio.wait_for(walk({'t': 'Str', 'c': 'again'}, leaf_min_len=1, min_len=1), 2))['s'] == 'ok'
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('positional', [False, True])
@pytest.mark.parametrize('exception', [ValueError, RuntimeError])
def test_originating_error_path_type_and_cause(monkeypatch, positional, exception):
    async def boom(*args, **kwargs):
        raise exception('backend failure')
    monkeypatch.setattr(sut, 'get_summary_response_async', boom)
    payload = {'t': 'Section', 'odd.key': {'t': 'Str', 'c': 'bad'}}
    async def scenario():
        if positional:
            return await sut.summarize_node_async(payload, Path('.'), object(), 'm', Path('doc.md'),
                                                 {'count': 0}, {'count': 0}, '$.blocks[0]', None, 1, 1, 'en')
        return await walk(payload, path='$.blocks[0]', leaf_min_len=1, min_len=1)
    with pytest.raises(ValueError) as caught:
        asyncio.run(scenario())
    assert 'Node type: Str' in str(caught.value)
    assert 'At path: $.blocks[0]["odd.key"]' in str(caught.value)
    assert isinstance(caught.value.__cause__, exception)


def test_image_language_errors_and_unsupported_shapes(monkeypatch):
    received = []
    async def image(**kwargs):
        received.append(kwargs['lang'])
        return 'image'
    monkeypatch.setattr(sut, 'get_image_summary_async', image)
    node = {'t': 'Image', 'c': [[], [], ['offline.png', '']]}
    assert asyncio.run(walk(node, lang='en'))['s'] == 'image'
    assert received == ['en']
    with pytest.raises(ValueError, match='Invalid image node structure') as caught:
        asyncio.run(walk({'t': 'Image', 'c': []}, path='$.image'))
    assert 'Node type: Image' in str(caught.value)
    assert 'At path: $.image' in str(caught.value)
    with pytest.raises(ValueError, match='Unsupported node type'):
        asyncio.run(walk([object()]))


def test_callback_scope_replacements_and_boundaries(monkeypatch):
    node = {'t': 'Section', 'c': [[True, None]], 'subnodes': {'t': 'Str', 'c': 'x'},
            'blank': [], 'scalar': 1, 'cite': {'t': 'Cite', 'c': ['ignored']},
            'image': {'t': 'Image', 'c': [[], [], ['offline.png', '']]}}
    async def image(**kwargs):
        return 'image'
    monkeypatch.setattr(sut, 'get_image_summary_async', image)
    visited = []
    def action(value):
        visited.append(type(value).__name__)
        if value == 'x':
            return 'changed'
        return value
    result = asyncio.run(walk(node, action=action))
    assert result['subnodes']['c'] == 'changed'
    assert visited.count('dict') == 4
    assert visited.count('list') == 3
    assert visited.count('bool') == 1
    assert visited.count('NoneType') == 1
    assert visited.count('int') == 1
    assert visited.count('str') == 1  # neither t nor generated s nor Cite content
    with pytest.raises(ValueError, match='preserve node container category'):
        asyncio.run(walk([], action=lambda value: {}))


@pytest.mark.parametrize('stale_artifact', [False, True])
def test_textualize_uses_memory_without_loading_ast_file(tmp_path, stale_artifact, monkeypatch):
    import chromadb
    from ribosome.core.dom.model import DOMClass

    source = tmp_path / 'doc.md'
    source.write_text('# title\n')
    dom = DOMClass(source, db_client=chromadb.EphemeralClient())
    monkeypatch.setattr(DOMClass, 'leaf_min_len', 9999)
    monkeypatch.setattr(DOMClass, 'min_len', 9999)
    dom.ast_json = json.dumps({'blocks': [{'t': 'Section', 'c': [{'t': 'Str', 'c': 'memory'}]}]})
    original = dom.ast_json
    artifact = tmp_path / 'doc_ast.json'
    artifact.write_bytes(b'not even valid json: intentionally stale')
    dom.ast_json_file = artifact if stale_artifact else None
    dom.semantics_json_file = tmp_path / 'doc_semantics.json'
    asyncio.run(dom.textualize())
    assert dom.ast_json == original
    assert artifact.read_bytes() == b'not even valid json: intentionally stale'
    result = json.loads(dom.semantics_json)
    assert result['blocks'][0]['s'] == 'memory'
    assert dom.semantics_json_file.read_bytes() == dom.semantics_json.encode('utf-8')


def test_mock_request_count_and_elapsed_time_are_separate_metrics(monkeypatch):
    calls = []
    async def request(client, text, **kwargs):
        calls.append(text)
        return 'summary'
    monkeypatch.setattr(sut, 'get_summary_response_async', request)
    start = perf_counter()
    result = asyncio.run(walk({'t': 'Section', 'c': [{'t': 'Str', 'c': 'text'}]}, leaf_min_len=1, min_len=1))
    elapsed = perf_counter() - start
    # Baseline: Str scalar, Str synthesis, list synthesis, Section synthesis.
    assert len(calls) == 4
    assert result['s'] == 'summary'
    assert elapsed >= 0  # no speedup claim; redundant-call changes are §7.4
