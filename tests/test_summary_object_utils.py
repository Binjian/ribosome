"""The extracted utilities work independently of the DOM summarization runtime."""
import asyncio
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from ribosome.core.dom.summary import object_utils
from ribosome.core.dom.summary import object as sut
from utils import summary_object_asyncio as batch


def test_utilities_import_without_loading_the_dom_summarizer():
    result = subprocess.run(
        [sys.executable, '-c',
         'import sys; from ribosome.core.dom.summary import object_utils; '
         'assert "ribosome.core.dom.summary.object" not in sys.modules; '
         'assert "ribosome.core.dom.utils" not in sys.modules'],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_row_summaries_use_the_supplied_runtime_and_provider():
    table = {'id': 1, 'source_file': 'manual.md', 'caption': 'Values',
             'content': '<table><tr><td rowspan="2">A</td><td>one</td></tr>'
                        '<tr><td>two</td></tr></table>'}
    table['children'], table['headers'] = object_utils._table_row_records(table, 1)
    requests, mapped_sizes = [], []
    async def scenario():
        semaphore = asyncio.Semaphore(1)
        async def ordered_map(function, values):
            mapped_sizes.append(len(values))
            return [await function(i, value) for i, value in enumerate(values)]
        async def summarize_response(client, content, model, role, lang):
            assert client == 'custom-client' and model == 'custom-model'
            assert role == 'user' and lang == 'en' and semaphore.locked()
            payload = json.loads(content.split('\n', 1)[1])
            requests.append(payload)
            return str(payload['row_index']) if 'row_index' in payload else '|'.join(payload['child_summaries'])
        return await object_utils._summarize_table_child_async(
            table['children'][0], table, 'custom-client', 'custom-model', 'en',
            ordered_map=ordered_map, get_semaphore=lambda: semaphore,
            summarize_response=summarize_response)
    group = asyncio.run(scenario())
    assert group['summary'] == '1|2'
    assert mapped_sizes == [2, 0, 0]
    assert [child['summary'] for child in group['children']] == ['1', '2']
    assert requests[-1]['child_summaries'] == ['1', '2']


@pytest.fixture(autouse=True)
def offline():
    token = sut._ignored_images.set(sut.object_utils._IgnoredImageSnapshot((), 'offline'))
    yield
    sut._ignored_images.reset(token)


def walk(node):
    return asyncio.run(sut.summarize_node_async(
        deepcopy(node), Path('.'), object(), 'offline', Path('doc.md'),
        {'count': 0}, {'count': 0}, leaf_min_len=1))


def test_leaf_once_and_single_child_skipped(monkeypatch):
    calls = []
    async def text(client, content, **kwargs):
        calls.append(content)
        return 'S<' + content + '>'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    result = walk({'t': 'Para', 'c': [{'t': 'Str', 'c': 'leaf'}]})
    assert result['s'] == '' and result['summary_skipped']
    assert result['c'][0]['s'] == 'S<leaf>'
    assert calls == ['leaf']


def test_first_per_direct_child_and_metadata_ignored(monkeypatch):
    calls = []
    async def text(client, content, **kwargs):
        calls.append(content)
        return content
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    tree = {'t': 'Content', 'caption': 'METADATA', 'c': [
        {'t': 'Wrapper', 'c': [], 'subnodes': [
            {'t': 'Para', 'c': [{'t': 'Str', 'c': 'first'}]},
            {'t': 'Para', 'c': [{'t': 'Str', 'c': 'second'}]}]},
        {'t': 'Str', 'c': 'third'}]}
    # First child has its own summary when it has multiple useful descendants.
    # Independently check the fallback on a previously skipped subtree.
    skipped = {'t': 'Div', 's': '', 'c': [], 'subnodes': [
        {'t': 'Str', 's': 'first'}, {'t': 'Str', 's': 'second'}]}
    assert sut.object_utils._summary_inputs([skipped, {'s': 'third'}]) == ['first', 'third']
    result = walk(tree)
    assert 'METADATA' not in result['s']
    assert calls[-1] == 'first second third'


@pytest.mark.parametrize('children', [[], [{'s': ''}], [{'s': '  '}, {'s': 'only'}]])
def test_insufficient_collection(children):
    assert sut.object_utils._summary_inputs(children) == []


def test_parent_text_is_independent_input(monkeypatch):
    async def text(client, content, **kwargs):
        return content
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    result = walk({'t': 'Para', 'c': ['intro', {'t': 'Str', 'c': 'child'}]})
    assert result['s'] == 'intro child'
    assert not result.get('summary_skipped')


def test_skipped_objects_resume_and_missing_leaf_is_filled(tmp_path, monkeypatch):
    source = tmp_path / 'manual.md'
    source.write_text('- leaf\n')
    calls = []
    async def text(**kwargs):
        calls.append(kwargs['content'])
        return 'LEAF'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    obj, = sut.extract_markdown_objects(source)
    async def summarize():
        return await sut._summarize_markdown_object_async(obj, None, 'offline', 'en', reuse_existing=True)
    asyncio.run(summarize())
    assert obj['summary'] == '' and obj['summary_skipped']
    assert len(calls) == 1
    asyncio.run(summarize())
    assert len(calls) == 1
    obj['children'][0]['summary'] = ''
    asyncio.run(summarize())
    assert len(calls) == 2 and obj['summary_skipped']
    wrapper = sut._row_summary_nodes({'children': [obj]})[0]
    assert wrapper['summary_skipped'] and batch._complete_object(obj, sut)


def test_ignored_and_mirrored_source_do_not_leak():
    node = {'t': 'Div', 's': '', 'object': {'children': [], 'summary_ignored': True},
            'c': [{'t': 'Str', 's': 'SOURCE'}]}
    assert sut.object_utils._first_summary(node) == ''


def test_document_skip_is_completed_and_reused(tmp_path, monkeypatch):
    source = tmp_path / 'manual.md'
    source.write_text('- leaf\n')
    extract = sut.extract_markdown_object_tree
    def untitled(path):
        tree = extract(path)
        tree['title'] = ''
        return tree
    monkeypatch.setattr(sut, 'extract_markdown_object_tree', untitled)
    calls = []
    async def text(**kwargs):
        calls.append(kwargs['content'])
        return 'LEAF'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    async def run():
        return await batch.summarize_object_documents(
            [source], object(), 'offline', checkpoint_path=tmp_path / 'checkpoint.json')
    row, = asyncio.run(run())
    assert row['status'] == 'completed'
    output = source.with_name('manual_semantics.json')
    previous = output.read_bytes()
    tree = json.loads(previous)
    assert tree['summary'] == '' and tree['summary_skipped']
    assert len(calls) == 1
    row, = asyncio.run(run())
    assert row['resumed'] and len(calls) == 1
    assert output.read_bytes() == previous


def test_legacy_entrypoint_uses_same_policy(monkeypatch):
    async def text(client, content, **kwargs):
        return content
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    node = {'t': 'Para', 'c': [{'t': 'Str', 'c': 'leaf'}]}
    actual = asyncio.run(sut.summary_node_async(
        deepcopy(node), Path('.'), Path('doc.md'), object(), 'offline', 0, 0))
    assert actual == walk(node)
