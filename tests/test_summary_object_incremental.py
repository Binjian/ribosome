"""Upgrade saved semantic trees using only missing row/item model requests."""
import asyncio
from copy import deepcopy
import json

import pytest

from ribosome.core.dom.summary import object as sut
from utils import summary_asyncio as shared
from utils import summary_object_asyncio as app


class Progress:
    instances = []
    def __init__(self, total, **kwargs):
        self.total, self.n, self.closed, self.status = total, 0, False, {}
        self.instances.append(self)
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def refresh(self): pass
    def set_postfix(self, **kwargs): self.status = kwargs
    def set_postfix_str(self, *args, **kwargs): pass
    @staticmethod
    def write(message): pass


def records(record):
    yield record
    for child in record.get('children', []):
        yield from records(child)


def wrappers(node):
    yield node
    for child in node.get('subnodes', []):
        yield from wrappers(child)


def assert_mirrors(node):
    assert node['s'] == node['object']['summary']
    assert [c['object'] for c in node.get('subnodes', [])] == node['object'].get('children', [])
    for child in node.get('subnodes', []):
        assert_mirrors(child)


def clear_summary_everywhere(value, identity):
    if isinstance(value, dict):
        if value.get('id') == identity and 'summary' in value:
            value['summary'] = ''
        if value.get('object', {}).get('id') == identity:
            value['s'] = ''
        for child in value.values():
            clear_summary_everywhere(child, identity)
    elif isinstance(value, list):
        for child in value:
            clear_summary_everywhere(child, identity)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path))
    monkeypatch.setattr(sut, '_SUMMARY_CONCURRENCY', 2)
    monkeypatch.setattr(sut, '_SUMMARY_TASK_LIMIT', 3)
    Progress.instances = []
    file = tmp_path / 'manual.md'
    file.write_text('# Manual\n\nExisting prose.\n\n'
                    '![diagram](diagram.png)\n图 1 Diagram\n\n'
                    '表 1 Values\n\n'
                    '<table><tr><td rowspan="3">A</td><td rowspan="2">B</td><td>one</td></tr>'
                    '<tr><td>two</td></tr><tr><td>three</td></tr></table>\n\n'
                    'Steps:\n\n3. Parent\n   - Child\n4. Sibling\n')
    output = file.with_name('manual_semantics.json')
    checkpoint = tmp_path / 'checkpoint.json'
    requests, images = [], []
    async def text(client, content, **kwargs):
        requests.append(content)
        return f'SAVED-{len(requests)}'
    async def image(client, image_link, **kwargs):
        images.append(image_link)
        return 'saved visual'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    monkeypatch.setattr(sut, 'get_image_summary_async', image)
    async def batch(**kwargs):
        return await app.summarize_object_documents(
            [file], object(), 'offline', checkpoint_path=checkpoint,
            progress_factory=Progress, progress_interval=60, leaf_min_len=99999, **kwargs)
    assert asyncio.run(batch())[0]['status'] == 'completed'
    original = json.loads(output.read_text())
    requests.clear()
    images.clear()
    return file, output, checkpoint, original, requests, images, batch


def save_legacy(case, mutate):
    file, output, checkpoint, original, *_ = case
    tree = deepcopy(original)
    mutate(tree)
    output.write_text(json.dumps(tree))
    saved = json.loads(checkpoint.read_text())
    saved['config']['settings']['object_schema_version'] = 2
    saved['entries'][str(file)]['signature'] = shared.SummaryCheckpoint.signature(file)
    checkpoint.write_text(json.dumps(saved))
    return tree, saved['entries'][str(file)]


def strip_children(tree):
    for node in sut._object_nodes(tree):
        if node['object']['type'] in {'table', 'list'}:
            node['object'].pop('children')
            node.pop('subnodes')


def test_upgrade_adds_every_missing_row_group_and_item_without_repeating_existing_work(case):
    file, output, checkpoint, original, requests, images, batch = case
    legacy, _ = save_legacy(case, strip_children)
    row, = asyncio.run(batch())
    assert row['status'] == 'completed' and row['incremental'] and not row['resumed']
    assert row['filled_summaries'] == len(requests) == 8  # Three rows, two groups, three items.
    assert row['object_count'] == row['completed_objects'] == 2
    assert not images
    updated = json.loads(output.read_text())
    assert updated['summary'] == legacy['summary']
    assert updated['blocks'][0]['s'] == legacy['blocks'][0]['s']
    for before, after in zip(sut._object_nodes(legacy), sut._object_nodes(updated)):
        assert before['c'] == after['c'] and before['s'] == after['s']
        if before['object']['type'] == 'figure':
            assert before == after
        assert_mirrors(after)
    for prompt in requests:
        assert prompt.startswith(('Summarize this table row', 'Summarize this merged table row group',
                                  'Summarize this list item'))
    saved = json.loads(checkpoint.read_text())
    assert saved['entries'][str(file)]['signature'] == shared.SummaryCheckpoint.signature(file)
    assert saved['entries'][str(file)]['row']['filled_summaries'] == 8
    previous = output.read_bytes()
    row, = asyncio.run(batch())
    assert row['resumed'] and len(requests) == 8 and not images
    assert output.read_bytes() == previous
    assert all(progress.closed for progress in Progress.instances)


@pytest.mark.parametrize('kind', ['table', 'list', 'table_row', 'table_row_group', 'list_item'])
def test_only_the_missing_summary_is_generated_even_under_summarized_parents(case, kind):
    _, output, _, original, requests, images, batch = case
    selected = {}
    def mutate(tree):
        for root in sut._object_nodes(tree):
            for node in wrappers(root):
                if node['object']['type'] == kind:
                    selected.update(node['object'])
                    # Records are also duplicated in ancestor group wrappers.
                    clear_summary_everywhere(tree, selected['id'])
                    return
    legacy, _ = save_legacy(case, mutate)
    previous = {record['id']: record['summary'] for root in sut._object_nodes(legacy)
                for record in records(root['object']) if record['id'] != selected['id']}
    row, = asyncio.run(batch())
    assert row['filled_summaries'] == len(requests) == 1 and not images
    updated = json.loads(output.read_text())
    assert updated['summary'] == original['summary']
    for root in sut._object_nodes(updated):
        assert_mirrors(root)
        for record in records(root['object']):
            if record['id'] != selected['id']:
                assert record['summary'] == previous[record['id']]


@pytest.mark.parametrize('missing_field', ['record', 'wrapper'])
def test_saved_wrapper_and_record_summaries_repair_each_other_without_requests(case, missing_field):
    _, output, _, original, requests, images, batch = case
    def mutate(tree):
        root = next(n for n in sut._object_nodes(tree) if n['object']['type'] == 'list')
        item = root['subnodes'][0]
        item['annotation'] = 'keep this'
        if missing_field == 'record':
            item['object']['summary'] = ''
            root['object']['children'][0]['summary'] = ''
        else:
            item['s'] = ''
    save_legacy(case, mutate)
    row, = asyncio.run(batch())
    assert row['incremental'] and row['filled_summaries'] == 0
    assert not requests and not images
    updated = json.loads(output.read_text())
    root = next(n for n in sut._object_nodes(updated) if n['object']['type'] == 'list')
    assert root['subnodes'][0]['annotation'] == 'keep this'
    assert_mirrors(root)


@pytest.mark.parametrize('missing', ['records', 'wrappers'])
def test_existing_descendant_summaries_are_recovered_from_either_saved_representation(case, missing):
    _, output, _, original, requests, images, batch = case
    def mutate(tree):
        for root in sut._object_nodes(tree):
            if root['object']['type'] in {'table', 'list'}:
                if missing == 'records':
                    root['object'].pop('children')
                else:
                    root.pop('subnodes')
    save_legacy(case, mutate)
    row, = asyncio.run(batch())
    assert row['incremental'] and row['filled_summaries'] == 0
    assert not requests and not images
    assert json.loads(output.read_text()) == original


def test_rows_saved_before_merged_groups_are_reused_in_the_new_hierarchy(case):
    _, output, _, original, requests, images, batch = case
    def mutate(tree):
        table = next(n for n in sut._object_nodes(tree) if n['object']['type'] == 'table')
        rows = [deepcopy(node) for node in wrappers(table) if node['object']['type'] == 'table_row']
        table['subnodes'] = rows
        table['object']['children'] = [node['object'] for node in rows]
    legacy, _ = save_legacy(case, mutate)
    row, = asyncio.run(batch())
    assert row['filled_summaries'] == len(requests) == 2 and not images
    assert all(prompt.startswith('Summarize this merged table row group') for prompt in requests)
    previous = {record['id']: record['summary'] for root in sut._object_nodes(legacy)
                for record in records(root['object'])}
    for root in sut._object_nodes(json.loads(output.read_text())):
        assert_mirrors(root)
        for record in records(root['object']):
            if record['id'] in previous:
                assert record['summary'] == previous[record['id']]


@pytest.mark.parametrize('outcome', ['failure', 'cancel', 'publication_failure'])
def test_failed_upgrade_preserves_output_and_completed_checkpoint_for_incremental_retry(case, monkeypatch, outcome):
    file, output, checkpoint, _, requests, images, batch = case
    _, entry = save_legacy(case, strip_children)
    previous = output.read_bytes()
    original_write = sut._write_semantics_tree
    original_summarize = sut._summarize_markdown_object_async
    active = set()
    async def scenario():
        started = asyncio.Event()
        async def text(client, content, **kwargs):
            active.add(content)
            try:
                started.set()
                if outcome == 'failure':
                    await asyncio.sleep(0)
                    raise RuntimeError('upgrade failed')
                if outcome == 'cancel':
                    await asyncio.Event().wait()
                return 'new summary'
            finally:
                active.remove(content)
        monkeypatch.setattr(sut, 'get_summary_response_async', text)
        if outcome == 'publication_failure':
            def fail(*args): raise OSError('publication failed')
            monkeypatch.setattr(sut, '_write_semantics_tree', fail)
        task = asyncio.create_task(batch())
        try:
            await asyncio.wait_for(started.wait(), 3)
            if outcome == 'cancel':
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
            else:
                row, = await asyncio.wait_for(task, 3)
                assert row['status'] == 'failed'
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert not active
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())
    assert output.read_bytes() == previous
    assert json.loads(checkpoint.read_text())['entries'][str(file)] == entry
    assert sut._summarize_markdown_object_async is original_summarize and not app._batch_active
    assert all(progress.closed for progress in Progress.instances)
    monkeypatch.setattr(sut, '_write_semantics_tree', original_write)
    async def text(client, content, **kwargs):
        requests.append(content)
        return 'retry summary'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    row, = asyncio.run(batch())
    assert row['status'] == 'completed' and row['incremental']
    assert len(requests) == 8 and not images


def test_complete_output_is_skipped_without_publication_or_requests(case, monkeypatch):
    _, output, checkpoint, _, requests, images, batch = case
    previous = output.read_bytes()
    entries = json.loads(checkpoint.read_text())['entries']
    def unexpected(*args): raise AssertionError('complete output should not be rewritten')
    monkeypatch.setattr(sut, '_write_semantics_tree', unexpected)
    row, = asyncio.run(batch())
    assert row['resumed'] and not requests and not images
    assert output.read_bytes() == previous
    assert json.loads(checkpoint.read_text())['entries'] == entries


def test_ignored_figures_remain_intentionally_empty_during_upgrade(case):
    _, output, _, _, requests, images, batch = case
    def mutate(tree):
        strip_children(tree)
        figure = next(sut._object_nodes(tree))
        figure['object'].update(summary='', summary_ignored=True)
        figure['object']['images'][0]['ignored_reference'] = 'saved ignored image'
        figure['s'] = ''
    legacy, _ = save_legacy(case, mutate)
    row, = asyncio.run(batch())
    assert row['filled_summaries'] == 8 and not images
    updated = json.loads(output.read_text())
    assert next(sut._object_nodes(updated)) == next(sut._object_nodes(legacy))


def test_explicit_full_rebuild_regenerates_existing_summaries(case):
    _, output, _, original, requests, images, batch = case
    save_legacy(case, strip_children)
    row, = asyncio.run(batch(resume=False))
    assert row['status'] == 'completed' and not row.get('incremental') and not row['resumed']
    assert len(requests) == 11 and len(images) == 1


def test_unknown_saved_descendants_fail_without_discarding_summaries(case):
    file, output, checkpoint, _, requests, images, batch = case
    def mutate(tree):
        root = next(n for n in sut._object_nodes(tree) if n['object']['type'] == 'list')
        child = deepcopy(root['object']['children'][-1])
        child['id'] = 'custom-saved-item'
        root['object']['children'].append(child)
    _, entry = save_legacy(case, mutate)
    previous = output.read_bytes()
    row, = asyncio.run(batch())
    assert row['status'] == 'failed' and 'cannot be preserved' in row['error']
    assert not requests and not images and output.read_bytes() == previous
    assert json.loads(checkpoint.read_text())['entries'][str(file)] == entry
