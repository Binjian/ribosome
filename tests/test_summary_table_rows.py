"""Table row extraction and bottom-up semantics, using offline providers."""
import asyncio
import json

import pytest

from ribosome.core.dom.summary import object as sut


def document(tmp_path, source):
    file = tmp_path / 'rows.md'
    file.write_text(source, encoding='utf-8')
    return file


def physical_rows(table):
    for child in table['children']:
        if child['type'] == 'table_row_group':
            yield from physical_rows(child)
        else:
            yield child


def merged_source():
    return ('# Section\n\n表 1 Categories\n\n<table>\n'
            '<thead><tr><th>Category</th><th>Value</th></tr></thead>\n'
            '<tbody>\n<tr><td rowspan="3">A</td><td>one</td></tr>\n'
            '<tr><td>two</td></tr>\n<tr><td>three</td></tr>\n'
            '<tr><td>B</td><td>four</td></tr>\n</tbody></table>\n')


def nested_source():
    return ('# Section\n\n表 1 Nested\n\n<table>\n'
            '<thead><tr><th>Category</th><th>Group</th><th>Subgroup</th><th>Value</th></tr></thead>\n'
            '<tbody>\n'
            '<tr><td rowspan="8">A</td><td rowspan="6">B</td><td>r2</td></tr>\n'
            '<tr><td rowspan="2">C</td><td>r3</td></tr>\n'
            '<tr><td>r4</td></tr>\n'
            '<tr><td>r5</td></tr>\n'
            '<tr><td rowspan="2">D</td><td>r6</td></tr>\n'
            '<tr><td>r7</td></tr>\n'
            '<tr><td rowspan="2">E</td><td>r8</td></tr>\n'
            '<tr><td>r9</td></tr>\n'
            '<tr><td>Tail</td><td>r10</td></tr>\n'
            '</tbody></table>\n')


def row_groups(table):
    for child in table['children']:
        if child['type'] == 'table_row_group':
            yield child
            yield from row_groups(child)


def test_nested_merges_preserve_three_levels_siblings_and_gaps(tmp_path):
    file = document(tmp_path, nested_source())
    before = file.read_bytes()
    table, = sut.extract_markdown_objects(file)
    header, outer, tail = table['children']
    assert header['row_index'] == 1 and tail['row_index'] == 10
    assert outer['id'] == '1.group.2-9'
    middle, sibling = outer['children']
    assert middle['id'] == '1.group.2-7' and sibling['id'] == '1.group.8-9'
    first, inner, gap, second = middle['children']
    assert first['row_index'] == 2 and gap['row_index'] == 5
    assert inner['id'] == '1.group.3-4' and second['id'] == '1.group.6-7'
    assert [r['row_index'] for r in inner['children']] == [3, 4]
    assert [r['row_index'] for r in second['children']] == [6, 7]
    assert [(g['row_start'], g['row_end']) for g in row_groups(table)] == [
        (2, 9), (2, 7), (3, 4), (6, 7), (8, 9)]
    for group in row_groups(table):
        assert group['line_start'] == group['row_start'] + 6
        assert group['line_end'] == group['row_end'] + 6
        assert group['content'] == '\n'.join(
            file.read_text().splitlines()[group['line_start'] - 1:group['line_end']])
    rows = list(physical_rows(table))
    assert [r['row_index'] for r in rows] == list(range(1, 11))
    assert len({r['id'] for r in rows}) == 10
    assert [c['text'] for c in rows[3]['cells'] if c['inherited']] == ['A', 'B', 'C']
    assert table == sut.extract_markdown_objects(file)[0]
    assert file.read_bytes() == before


def test_identical_ranges_do_not_add_redundant_group_levels(tmp_path):
    source = nested_source().replace('<td rowspan="8">A</td>',
                                    '<td rowspan="8" colspan="2">A</td><td rowspan="8">Copy</td>')
    table, = sut.extract_markdown_objects(document(tmp_path, source))
    groups = list(row_groups(table))
    assert len(groups) == len({g['id'] for g in groups}) == 5
    rows = list(physical_rows(table))
    assert [r['row_index'] for r in rows] == list(range(1, 11))
    assert [(c['text'], c['column'], c['colspan']) for c in rows[3]['cells']] == [
        ('A', 1, 2), ('Copy', 3, 1), ('B', 4, 1), ('C', 5, 1), ('r4', 6, 1)]


@pytest.mark.parametrize('span', [0, 99])
def test_nested_groups_stop_at_html_row_group_boundaries(tmp_path, span):
    source = nested_source().replace('rowspan="8"', f'rowspan="{span}"')
    source = source.replace('<tr><td>Tail', '</tbody><tbody><tr><td>Tail')
    table, = sut.extract_markdown_objects(document(tmp_path, source))
    assert [g['id'] for g in row_groups(table)] == [
        '1.group.2-9', '1.group.2-7', '1.group.3-4', '1.group.6-7', '1.group.8-9']
    tail = table['children'][-1]
    assert tail['row_index'] == 10
    assert not any(c['inherited'] for c in tail['cells'])


def test_pipe_rows_keep_source_headers_escaped_pipes_and_empty_cells(tmp_path):
    file = document(tmp_path, '表 1 Values\n\n| Name | Value |\n| :--- | ---: |\n'
                    '| a\\|b | `code` |\n| [link](url) | |\n')
    before = file.read_bytes()
    table, = sut.extract_markdown_objects(file)
    rows = table['children']
    assert [r['id'] for r in rows] == ['1.row.1', '1.row.2', '1.row.3']
    assert [(r['line_start'], r['line_end']) for r in rows] == [(3, 3), (5, 5), (6, 6)]
    assert [r['is_header'] for r in rows] == [True, False, False]
    assert [[c['text'] for c in r['cells']] for r in rows] == [
        ['Name', 'Value'], ['a|b', 'code'], ['link', '']]
    assert table['headers'] == [rows[0]['cells']]
    assert all(r['caption'] == '表 1 Values' and not r['summary'] for r in rows)
    assert rows[1]['content'] == '| a\\|b | `code` |\n'
    assert sut.extract_markdown_objects(file) == [table]
    assert file.read_bytes() == before


def test_html_merges_inherit_values_and_form_ordered_groups(tmp_path):
    table, = sut.extract_markdown_objects(document(tmp_path, merged_source()))
    assert [c['type'] for c in table['children']] == ['table_row', 'table_row_group', 'table_row']
    header, group, last = table['children']
    assert group['id'] == '1.group.2-4'
    assert (group['row_start'], group['row_end']) == (2, 4)
    assert (group['line_start'], group['line_end']) == (8, 10)
    rows = list(physical_rows(table))
    assert [r['row_index'] for r in rows] == [1, 2, 3, 4, 5]
    assert [c['text'] for c in rows[2]['cells']] == ['A', 'two']
    inherited = rows[2]['cells'][0]
    assert (inherited['source_row'], inherited['column'], inherited['rowspan']) == (2, 1, 3)
    assert inherited['inherited'] and not rows[1]['cells'][0]['inherited']
    assert [c['text'] for c in last['cells']] == ['B', 'four']
    assert table['headers'] == [header['cells']]


def test_overlapping_and_contained_spans_combine_without_duplicate_rows(tmp_path):
    source = ('<table><tr><td rowspan="3" colspan="2">A</td><td>x</td></tr>'
              '<tr><td rowspan="3">B</td><td rowspan="2">C</td></tr>'
              '<tr><td>y</td></tr><tr><td>D</td><td>E</td><td>z</td></tr>'
              '<tr><td>last</td></tr></table>')
    table, = sut.extract_markdown_objects(document(tmp_path, source))
    group, last = table['children']
    assert group['type'] == 'table_row_group'
    assert (group['row_start'], group['row_end']) == (1, 4)
    assert len(group['children']) == 3 and last['row_index'] == 5
    assert group['children'][1]['id'] == '1.group.2-3'
    assert [r['row_index'] for r in group['children'][1]['children']] == [2, 3]
    rows = list(physical_rows(table))
    assert len({r['id'] for r in rows}) == 5
    assert [(c['text'], c['column'], c['colspan']) for c in rows[2]['cells']] == [
        ('A', 1, 2), ('B', 3, 1), ('C', 4, 1), ('y', 5, 1)]
    assert [c['text'] for c in rows[3]['cells']] == ['D', 'E', 'B', 'z']


def test_nested_tables_are_cell_content_and_same_line_tables_keep_locations(tmp_path):
    source = ('Prose.\n\n<table><tr><td>outer<table><tr><td>inner</td></tr></table>'
              '</td><td>&amp; end</td></tr></table>'
              '<table><tr><td>second</td></tr></table>\n')
    tables = sut.extract_markdown_objects(document(tmp_path, source))
    assert len(tables) == 2
    assert [len(t['children']) for t in tables] == [1, 1]
    first = tables[0]['children'][0]
    assert first['cells'][0]['text'] == 'outer inner'
    assert '<table>' in first['cells'][0]['content']
    assert first['cells'][1]['text'] == '& end'
    assert [(t['children'][0]['line_start'], t['children'][0]['line_end']) for t in tables] == [(3, 3)] * 2


def test_rowspan_zero_stops_at_row_group_and_optional_end_tags_work(tmp_path):
    source = ('<table><tbody><tr><td rowspan="0">A<td>one'
              '<tr><td>two</tbody><tbody><tr><td>B<td>three</tbody></table>')
    table, = sut.extract_markdown_objects(document(tmp_path, source))
    group, last = table['children']
    assert len(group['children']) == 2 and last['row_index'] == 3
    assert [c['text'] for c in group['children'][1]['cells']] == ['A', 'two']
    assert [c['text'] for c in last['cells']] == ['B', 'three']


@pytest.mark.parametrize('span', ['-1', 'invalid', '1'])
def test_non_merging_span_values_and_empty_html_rows(tmp_path, span):
    table, = sut.extract_markdown_objects(document(tmp_path,
        f'<table><tr><td rowspan="{span}" colspan="0">A</td></tr><tr></tr></table>'))
    assert [r['type'] for r in table['children']] == ['table_row', 'table_row']
    assert table['children'][0]['cells'][0]['colspan'] == 1
    assert table['children'][1]['cells'] == []


def test_pipe_rows_use_document_reference_definitions(tmp_path):
    table, = sut.extract_markdown_objects(document(tmp_path,
        '| Header |\n| --- |\n| [label][ref] |\n\n[ref]: https://example.com\n'))
    assert table['children'][1]['cells'][0]['text'] == 'label'


@pytest.mark.parametrize('source', ['<table></table>', '| Header |\n| --- |\n'])
def test_empty_and_header_only_tables(tmp_path, source):
    table, = sut.extract_markdown_objects(document(tmp_path, source))
    assert len(table['children']) == (0 if source.startswith('<') else 1)


def test_row_group_and_table_summaries_flow_into_recursive_json(tmp_path, monkeypatch):
    file = document(tmp_path, merged_source())
    original = sut.extract_markdown_object_tree(file)
    original_table = next(sut._object_nodes(original))
    prompts = []
    async def text(client, content, **kwargs):
        assert kwargs['lang'] == 'en' and kwargs['model'] == 'offline'
        instruction, raw = content.split('\n', 1)
        payload = json.loads(raw)
        prompts.append((instruction, payload))
        if 'row_index' in payload:
            assert payload['caption'] == '表 1 Categories'
            assert [c['text'] for c in payload['headers'][0]] == ['Category', 'Value']
            if payload['row_index'] == 3:
                assert payload['cells'][0]['text'] == 'A' and payload['cells'][0]['inherited']
            return f"ROW-{payload['row_index']}"
        return '|'.join(payload['child_summaries'])
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    result = asyncio.run(sut.summarize_markdown_object_tree_async(
        file, object(), 'offline', lang='en', leaf_min_len=99999))
    table = next(sut._object_nodes(result))
    assert table['c'] == original_table['c']
    assert len(prompts) == 7  # Five rows, their merged group, and the table.
    assert prompts[-1][1]['child_summaries'] == ['ROW-1', 'ROW-2|ROW-3|ROW-4', 'ROW-5']
    group_prompt = next(p for i, p in prompts if 'row group' in i)
    assert group_prompt['child_summaries'] == ['ROW-2', 'ROW-3', 'ROW-4']
    for row in physical_rows(table['object']):
        assert row['summary'] in table['s']
        assert row['summary'] in result['summary']
    def check_wrappers(node):
        assert node['s'] == node['object']['summary']
        assert [c['object'] for c in node.get('subnodes', [])] == node['object'].get('children', [])
        for child in node.get('subnodes', []):
            check_wrappers(child)
    check_wrappers(table)
    destination = tmp_path / 'rows_semantics.json'
    sut._write_semantics_tree(destination, result)
    assert json.loads(destination.read_text()) == result
    # Both APIs return the same table record, with headers, groups and leaf summaries.
    flat = asyncio.run(sut.summarize_markdown_objects_async(file, object(), 'offline', lang='en'))
    assert flat[0] == table['object']


def test_every_nested_subgroup_is_summarized_bottom_up_and_embedded(tmp_path, monkeypatch):
    file = document(tmp_path, nested_source())
    original = sut.extract_markdown_object_tree(file)
    calls, bars = [], []
    class Bar:
        def __init__(self, **kwargs):
            self.total, self.completed, self.closed = kwargs['total'], 0, False
            bars.append(self)
        def update(self, amount): self.completed += amount
        def close(self): self.closed = True
    async def text(client, content, **kwargs):
        instruction, raw = content.split('\n', 1)
        payload = json.loads(raw)
        if 'row_index' in payload:
            identity = f"1.row.{payload['row_index']}"
            summary = f"R{payload['row_index']}"
        elif 'row_start' in payload:
            start, end = payload['row_start'], payload['row_end']
            identity = f'1.group.{start}-{end}'
            if (start, end) in {(3, 4), (6, 7)}:
                assert [c['text'] for c in payload['inherited_cells']] == ['A', 'B']
            elif (start, end) == (8, 9):
                assert [c['text'] for c in payload['inherited_cells']] == ['A']
            summary = f"G{start}-{end}(" + '|'.join(payload['child_summaries']) + ')'
        else:
            identity = '1'
            summary = 'TABLE(' + '|'.join(payload['child_summaries']) + ')'
        calls.append((identity, payload))
        await asyncio.sleep(0)
        return summary
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    monkeypatch.setattr('tqdm.auto.tqdm', Bar)
    tree = asyncio.run(sut.summarize_markdown_object_tree_async(
        file, object(), 'offline', show_progress=True, leaf_min_len=99999))
    wrapper = next(sut._object_nodes(tree))
    assert wrapper['c'] == next(sut._object_nodes(original))['c']
    assert len(calls) == len({identity for identity, _ in calls}) == 16
    indices = {identity: i for i, (identity, _) in enumerate(calls)}
    payloads = dict(calls)
    def check(node):
        record = node['object']
        assert node['s'] == record['summary'] and node['s'].strip()
        assert [c['object'] for c in node.get('subnodes', [])] == record['children']
        if record['children']:
            assert payloads[str(record['id'])]['child_summaries'] == [c['summary'] for c in record['children']]
        for child in node.get('subnodes', []):
            assert indices[child['object']['id']] < indices[str(record['id'])]
            assert child['s'] in node['s']
            check(child)
    check(wrapper)
    assert wrapper['s'] in tree['summary']
    assert bars[0].total == bars[0].completed == 1 and bars[0].closed
    destination = tmp_path / 'nested_semantics.json'
    sut._write_semantics_tree(destination, tree)
    assert json.loads(destination.read_text()) == tree
    flat = asyncio.run(sut.summarize_markdown_objects_async(file, object(), 'offline'))
    assert flat[0] == wrapper['object']


@pytest.mark.parametrize('target', ['row', 'group'])
@pytest.mark.parametrize('failure', ['empty', 'error'])
def test_deep_failures_locate_origin_and_cancel_siblings_without_publishing(tmp_path, monkeypatch, target, failure):
    file = document(tmp_path, nested_source())
    destination = tmp_path / 'nested_semantics.json'
    destination.write_text('previous')
    updates, active = [], set()
    class Progress:
        def update(self, amount): updates.append(amount)
    async def text(client, content, **kwargs):
        identity = object()
        active.add(identity)
        try:
            payload = json.loads(content.split('\n', 1)[1])
            await asyncio.sleep(0)
            selected = (payload.get('row_index') == 4 if target == 'row' else
                        (payload.get('row_start'), payload.get('row_end')) == (3, 4))
            if selected:
                if failure == 'error': raise RuntimeError('deep provider failure')
                return ''
            return 'summary'
        finally:
            active.remove(identity)
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    table, = sut.extract_markdown_objects(file)
    async def run():
        await sut._summarize_markdown_object_async(table, object(), 'offline', 'en', Progress())
        sut._write_semantics_tree(destination, {'blocks': [table]})
    with pytest.raises(ValueError) as exc:
        asyncio.run(run())
    message = str(exc.value)
    assert 'table object 1' in message and '1.group.2-9' in message and '1.group.2-7' in message
    assert 'table_row_group 1.group.3-4' in message
    assert ('rows.md:10-10' if target == 'row' else 'rows.md:9-10') in message
    assert not active and not updates and destination.read_text() == 'previous'


@pytest.mark.parametrize('stage', ['row', 'group', 'table'])
@pytest.mark.parametrize('failure', ['empty', 'error'])
def test_failures_identify_rows_and_do_not_publish_or_complete_table(tmp_path, monkeypatch, stage, failure):
    file = document(tmp_path, merged_source())
    destination = tmp_path / 'rows_semantics.json'
    destination.write_text('previous')
    updates = []
    class Progress:
        def update(self, amount): updates.append(amount)
    table, = sut.extract_markdown_objects(file)
    async def text(client, content, **kwargs):
        current = ('group' if content.startswith('Summarize this merged') else
                   'table' if content.startswith('Summarize this complete') else 'row')
        await asyncio.sleep(0)
        if current == stage:
            if failure == 'error': raise RuntimeError('provider failed')
            return ' '
        return 'summary'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    async def run():
        await sut._summarize_markdown_object_async(table, object(), 'offline', 'en', Progress())
        sut._write_semantics_tree(destination, {'blocks': [table]})
    with pytest.raises(ValueError) as exc:
        asyncio.run(run())
    assert 'table object 1' in str(exc.value) and 'rows.md:' in str(exc.value)
    if stage != 'table':
        assert ('table_row_group 1.group.2-4' if stage == 'group' else 'table_row 1.row.') in str(exc.value)
    assert not updates and destination.read_text() == 'previous'


@pytest.mark.parametrize('limit', [1, 2])
@pytest.mark.parametrize('source', [merged_source, nested_source])
def test_merged_row_concurrency_stays_bounded_and_cancellation_cleans_up(tmp_path, monkeypatch, limit, source):
    file = document(tmp_path, source())
    monkeypatch.setattr(sut, '_SUMMARY_CONCURRENCY', limit)
    monkeypatch.setattr(sut, '_SUMMARY_TASK_LIMIT', limit)
    active = peak = 0
    async def text(client, content, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(0.001)
            return 'summary'
        finally:
            active -= 1
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    asyncio.run(sut.summarize_markdown_objects_async(file, object(), 'offline'))
    assert active == 0 and peak == limit
    async def scenario():
        started = asyncio.Event()
        async def blocked(client, content, **kwargs):
            nonlocal active
            active += 1
            try:
                payload = json.loads(content.split('\n', 1)[1])
                if (payload.get('row_index') == 2 if source is merged_source else
                        (payload.get('row_start'), payload.get('row_end')) == (3, 4)):
                    started.set()
                    await asyncio.Event().wait()
                return 'summary'
            finally:
                active -= 1
        monkeypatch.setattr(sut, 'get_summary_response_async', blocked)
        task = asyncio.create_task(sut.summarize_markdown_objects_async(file, object(), 'offline'))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(scenario())
    assert active == 0
