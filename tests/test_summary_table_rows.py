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
            yield from child['children']
        else:
            yield child


def merged_source():
    return ('# Section\n\n表 1 Categories\n\n<table>\n'
            '<thead><tr><th>Category</th><th>Value</th></tr></thead>\n'
            '<tbody>\n<tr><td rowspan="3">A</td><td>one</td></tr>\n'
            '<tr><td>two</td></tr>\n<tr><td>three</td></tr>\n'
            '<tr><td>B</td><td>four</td></tr>\n</tbody></table>\n')


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
    assert len(group['children']) == 4 and last['row_index'] == 5
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
def test_merged_row_concurrency_stays_bounded_and_cancellation_cleans_up(tmp_path, monkeypatch, limit):
    file = document(tmp_path, merged_source())
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
                if 'row_index": 2' in content:
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
