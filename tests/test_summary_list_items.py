"""Recursive list item extraction and bottom-up summaries with offline providers."""
import asyncio
import json

import pytest

from ribosome.core.dom.summary import object as sut


def document(tmp_path, source=None):
    file = tmp_path / 'manual.md'
    file.write_text(source if source is not None else (
        '# Manual\n\nSteps:\n\n'
        '3. **First** [guide][ref]\n\n'
        '   Continued instructions.\n\n'
        '   - Child A\n'
        '     - Grandchild\n'
        '   - Child B\n\n'
        '4. Second\n\n'
        '   7. Numbered child\n'
        '   8. Other child\n\n'
        '[ref]: https://example.test/guide\n'), encoding='utf-8')
    return file


def descendants(record):
    for child in record.get('children', []):
        yield child
        yield from descendants(child)


def check_wrappers(node):
    assert node['s'] == node['object']['summary']
    assert [c['object'] for c in node.get('subnodes', [])] == node['object']['children']
    for child in node.get('subnodes', []):
        check_wrappers(child)


def test_list_items_preserve_source_order_spans_numbering_and_nesting(tmp_path):
    file = document(tmp_path)
    before = file.read_bytes()
    record, = sut.extract_markdown_objects(file)
    assert record['type'] == 'list' and record['caption'] == 'Steps:'
    first, second = record['children']
    assert [c['ordinal'] for c in record['children']] == [3, 4]
    assert [c['list_start'] for c in record['children']] == [3, 3]
    assert [c['item_index'] for c in record['children']] == [1, 2]
    assert [c['list_type'] for c in record['children']] == ['ordered', 'ordered']
    assert [c['list_type'] for c in first['children']] == ['bullet', 'bullet']
    assert [c['ordinal'] for c in first['children']] == [None, None]
    assert len(first['children'][0]['children']) == 1
    assert [c['ordinal'] for c in second['children']] == [7, 8]
    assert 'Continued instructions.' in first['content'] and '[guide][ref]' in first['content']
    items = list(descendants(record))
    assert len(items) == len({item['id'] for item in items}) == 7
    assert first['id'] == '1.item.1'
    assert first['children'][0]['children'][0]['id'] == '1.item.1.item.1.item.1'
    lines = file.read_text().splitlines(keepends=True)
    for parent in [record, *items]:
        for child in parent['children']:
            assert child['type'] == 'list_item' and child['summary'] == ''
            assert child['list_id'] == record['id'] and child['parent_id'] == parent['id']
            assert child['source_file'] == str(file.resolve()) and child['caption'] == 'Steps:'
            assert child['content'] == ''.join(lines[child['line_start'] - 1:child['line_end']])
    assert file.read_bytes() == before
    assert sut.extract_markdown_objects(file) == [record]


def test_multiple_nested_lists_and_code_blocks_do_not_duplicate_items(tmp_path):
    file = document(tmp_path, '- Parent\n\n'
                    '  - Bullet child\n\n'
                    '  More prose.\n\n'
                    '  5. Ordered child\n\n'
                    '  ```md\n  - fake child\n  ```\n\n'
                    '- Sibling\n')
    record, = sut.extract_markdown_objects(file)
    first, second = record['children']
    assert len(list(descendants(record))) == 4
    assert [c['id'] for c in first['children']] == ['1.item.1.item.1', '1.item.1.item.2']
    assert [c['item_index'] for c in first['children']] == [1, 1]
    assert [c['list_type'] for c in first['children']] == ['bullet', 'ordered']
    assert second['children'] == [] and 'fake child' in first['content']


@pytest.mark.parametrize('source', ['-\n-\n', '9.\n10.\n', '- same\n- same\n'])
def test_empty_and_duplicate_items_remain_distinct(tmp_path, source):
    record, = sut.extract_markdown_objects(document(tmp_path, source))
    assert len(record['children']) == 2
    assert [c['id'] for c in record['children']] == ['1.item.1', '1.item.2']
    assert [c['line_start'] for c in record['children']] == [1, 2]


@pytest.mark.parametrize('start', [0, 1])
def test_ordered_lists_preserve_zero_and_default_starts(tmp_path, start):
    record, = sut.extract_markdown_objects(document(tmp_path, f'{start}. first\n{start + 1}. second\n'))
    assert [c['list_start'] for c in record['children']] == [start, start]
    assert [c['ordinal'] for c in record['children']] == [start, start + 1]


@pytest.mark.parametrize('tree_output', [False, True])
def test_item_summaries_flow_bottom_up_and_mirror_recursive_json(tmp_path, monkeypatch, tree_output):
    file = document(tmp_path)
    original = sut.extract_markdown_object_tree(file)
    original_wrapper, = sut._object_nodes(original)
    check_wrappers(original_wrapper)
    prompts, completed = {}, {}

    async def text(client, content, model, role, lang):
        assert client == 'custom-client' and model == 'custom-model' and role == 'user' and lang == 'en'
        instruction, raw = content.split('\n', 1)
        payload = json.loads(raw)
        assert payload['caption'] == 'Steps:'
        if 'item_index' in payload:
            record = next(item for item in descendants(original_wrapper['object']) if item['id'] == payload['id'])
            assert payload['content'] == record['content']
            assert payload['child_summaries'] == [completed[c['id']] for c in record['children']]
            assert 'list item' in instruction
            summary = payload['id'] + '(' + '|'.join(payload['child_summaries']) + ')'
            prompts[payload['id']] = payload
            completed[payload['id']] = summary
            return summary
        assert payload['child_summaries'] == [completed[c['id']] for c in original_wrapper['object']['children']]
        prompts['list'] = payload
        return 'LIST(' + '|'.join(payload['child_summaries']) + ')'

    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    if tree_output:
        result = asyncio.run(sut.summarize_markdown_object_tree_async(
            file, 'custom-client', 'custom-model', lang='en', leaf_min_len=99999))
        wrapper, = sut._object_nodes(result)
        check_wrappers(wrapper)
        assert wrapper['c'] == original_wrapper['c']
        record = wrapper['object']
        assert record['summary'] in result['summary']
        destination = tmp_path / 'manual_semantics.json'
        sut._write_semantics_tree(destination, result)
        restored = json.loads(destination.read_text())
        check_wrappers(next(sut._object_nodes(restored)))
    else:
        record, = asyncio.run(sut.summarize_markdown_objects_async(
            file, 'custom-client', 'custom-model', lang='en'))
    assert len(prompts) == 8  # Seven items, including nested items, and the list.
    for item in descendants(record):
        assert item['summary'] == completed[item['id']]
        assert item['summary'] in record['summary']


@pytest.mark.parametrize('empty', ['', '  ', None, 42])
def test_empty_item_summary_reports_item_and_source_location(tmp_path, monkeypatch, empty):
    file = document(tmp_path, 'Steps:\n\n- parent\n  - child\n')
    async def text(**kwargs):
        return empty
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    with pytest.raises(ValueError, match=r'list_item 1.item.1.item.1 in list 1 at .*manual.md:4-4: Provider returned an empty list item summary'):
        asyncio.run(sut.summarize_markdown_objects_async(file, object(), 'offline'))


@pytest.mark.parametrize('limit', [1, 2])
def test_recursive_item_requests_respect_shared_limits(tmp_path, monkeypatch, limit):
    file = document(tmp_path)
    active = peak = 0
    async def text(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001)
            return 'summary'
        finally:
            active -= 1
    monkeypatch.setattr(sut, '_SUMMARY_CONCURRENCY', limit)
    monkeypatch.setattr(sut, '_SUMMARY_TASK_LIMIT', limit)
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    async def run():
        return await asyncio.wait_for(sut.summarize_markdown_objects_async(file, object(), 'offline'), 3)
    record, = asyncio.run(run())
    assert peak == limit and active == 0
    assert all(item['summary'] == 'summary' for item in descendants(record))


@pytest.mark.parametrize('outcome', ['failure', 'cancel'])
def test_nested_item_failure_and_cancellation_await_sibling_cleanup(tmp_path, monkeypatch, outcome):
    file = document(tmp_path, '- parent\n  - bad\n  - blocked\n- sibling\n')
    active = set()
    async def run():
        blocked = asyncio.Event()
        async def text(**kwargs):
            payload = json.loads(kwargs['content'].split('\n', 1)[1])
            identity = payload['id']
            active.add(identity)
            try:
                if identity == '1.item.1.item.1' and outcome == 'failure':
                    await blocked.wait()
                    raise RuntimeError('provider failed')
                blocked.set()
                await asyncio.Event().wait()
            finally:
                active.remove(identity)
        monkeypatch.setattr(sut, 'get_summary_response_async', text)
        task = asyncio.create_task(sut.summarize_markdown_objects_async(file, object(), 'offline'))
        try:
            await asyncio.wait_for(blocked.wait(), 3)
            if outcome == 'cancel':
                task.cancel()
            with pytest.raises(ValueError if outcome == 'failure' else asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not active
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(run())
