"""Offline task-table classification, with saved semantic DOM fixtures."""
from copy import deepcopy
import json
import subprocess
import sys

import pytest

from ribosome.core.dom.summary.object_utils import _table_row_records, _row_summary_nodes
from ribosome.core.dom.summary.task import (
    classify_task_objects, task_match_reasons, task_object_records,
)


def table(identity=1, content=None, *, caption='', metadata=True):
    obj = dict(id=identity, type='table', source_file='unavailable/manual.md',
               line_start=10, line_end=20, caption=caption, images=[],
               summary=f'Existing table {identity}',
               content=content or '<table><tr><td>NOP</td><td>开始</td></tr></table>')
    if metadata:
        obj['children'], obj['headers'] = _table_row_records(obj, 10)
    return obj


def wrapper(obj):
    # JSON-decoded trees contain distinct copies of every mirrored descendant.
    return json.loads(json.dumps(dict(t='Div', c=[['', [], []], [
        {'t': 'RawBlock', 'c': ['html', obj['content']]},
    ]], object=obj, s=obj['summary'], subnodes=_row_summary_nodes(obj))))


def para(text):
    return {'t': 'Para', 'c': [{'t': 'Str', 'c': text}], 's': 'Existing prose'}


def section(title, body=(), subsections=()):
    return dict(t='Section', c=[
        {'t': 'Header', 'c': [2, ['ignored-id', [], []], [
            {'t': 'Strong', 'c': [{'t': 'Str', 'c': title}]},
        ]], 's': 'Existing header'},
        {'t': 'Content', 'c': list(body), 's': 'Existing content'},
    ], subnodes=list(subsections), s='Existing section')


def document(*blocks):
    return dict(blocks=list(blocks), file_path='unavailable/manual.md',
                title='Manual', summary='Existing document', meta={})


def all_records(value):
    if isinstance(value, dict):
        if 'id' in value and 'type' in value:
            yield value
        for child in value.values():
            yield from all_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_records(child)


@pytest.mark.parametrize('term', ['作业模板', '模板作业', '作业示例', '示例作业'])
def test_section_terms_include_nested_subsections_without_leaking_to_siblings(term):
    tree = document(section(f'3 {term}配置', [wrapper(table(1))], [
        section('3.1 Child', [wrapper(table(2))], [
            section('3.1.1 Grandchild', [wrapper(table(3))]),
        ]),
    ]), section('4 Other', [wrapper(table(4))]))
    actual = classify_task_objects(tree)
    assert {obj['id']: obj['type'] for obj in all_records(actual)
            if obj['type'] in {'table', 'task'}} == {1: 'task', 2: 'task', 3: 'task', 4: 'table'}
    records = task_object_records(tree)
    assert [obj['id'] for obj in records] == [1, 2, 3]
    assert all(obj['reasons'] == ('section_title',) for obj in records)
    assert records[-1]['section_path'] == (f'3 {term}配置', '3.1 Child', '3.1.1 Grandchild')


@pytest.mark.parametrize('source', [
    '<table><tr><td>作业</td><td>说明</td></tr><tr><td>NOP</td><td>开始</td></tr></table>',
    '<table><thead><tr><th>说明</th><th><strong>机器人作&#19994;名</strong></th></tr></thead></table>',
    '| 说明 | **机器人作业名** |\n| --- | --- |\n| 开始 | NOP |\n',
    '<table><tr><td rowspan="2">作业</td><td>说明</td></tr><tr><td>开始</td></tr></table>',
])
@pytest.mark.parametrize('metadata', [False, True])
def test_first_row_header_with_and_without_saved_rows(source, metadata):
    obj = table(content=source, metadata=metadata)
    before = deepcopy(obj)
    assert task_match_reasons(obj) == ('header_row',)
    assert obj == before
    actual = classify_task_objects(document(wrapper(obj)))
    assert actual['blocks'][0]['object']['type'] == 'task'
    assert ('children' in actual['blocks'][0]['object']) == metadata


@pytest.mark.parametrize('source', [
    '<table><thead><tr><th>编号</th><th>说明</th></tr>'
    '<tr><th>1</th><th>作业错误</th></tr></thead></table>',
    '<table><tr><td rowspan="2">编号</td><td>说明</td></tr><tr><td>作业错误</td></tr></table>',
    '| 编号 | 说明 |\n| --- | --- |\n| 1 | 作业错误 |\n',
])
def test_body_occurrences_are_not_headers(source):
    obj = table(content=source)
    assert task_match_reasons(obj) == ()
    tree = document(wrapper(obj))
    assert classify_task_objects(tree) == tree


@pytest.mark.parametrize('first,second,expected', [
    ('作业', 'NOP', ('header_row',)),
    ('编号', '作业错误', ()),
])
def test_incomplete_saved_rows_fall_back_to_original_first_row(first, second, expected):
    obj = table(content=f'<table><tr><td>{first}</td></tr><tr><td>{second}</td></tr></table>')
    obj['children'] = obj['children'][1:]
    before = deepcopy(obj)
    assert task_match_reasons(obj) == expected
    assert obj == before


@pytest.mark.parametrize('term', ['子作业参数表', '基本作业流程'])
@pytest.mark.parametrize('title_location', ['caption', 'paragraph', 'heading', 'section'])
def test_direct_title_terms_and_formats(term, title_location):
    obj = table(caption=f'表 2 {term}' if title_location == 'caption' else '')
    block = wrapper(obj)
    if title_location == 'paragraph':
        title = para('')
        title['c'] = [{'t': 'Strong', 'c': [{'t': 'Str', 'c': f'表2‑3 {term}'}]}]
        tree = document(title, para('  '), {'t': 'Null'}, block)
    elif title_location == 'heading':
        tree = document(section(term)['c'][0], block)
    elif title_location == 'section':
        tree = document(section(term, [block, wrapper(table(2))]))
    else:
        tree = document(block)
    records = task_object_records(tree)
    assert [r['id'] for r in records] == [1]
    assert records[0]['reasons'] == ('preceding_title',)
    actual = classify_task_objects(tree)
    assert next(o for o in all_records(actual) if o['id'] == 1)['type'] == 'task'


@pytest.mark.parametrize('intervening', [
    para('Other text'),
    {'t': 'HorizontalRule'},
    wrapper(dict(table(99), type='figure')),
])
def test_intervening_content_breaks_title_adjacency(intervening):
    tree = document(para('子作业参数表'), intervening, wrapper(table()))
    assert classify_task_objects(tree) == tree


def test_caption_precedence_and_section_heading_not_reused():
    tree = document(para('子作业参数表'), wrapper(table(caption='表 1 Other')),
                    section('基本作业流程', [para('Intro'), wrapper(table(2))], [
                        section('Child', [wrapper(table(3))]),
                    ]))
    assert classify_task_objects(tree) == tree


def test_link_target_attributes_body_and_summaries_do_not_supply_titles():
    title = para('')
    title['c'] = [{'t': 'Link', 'c': [
        ['子作业参数表', [], []], [{'t': 'Str', 'c': 'Other'}], ['基本作业流程', '子作业参数表'],
    ]}]
    tree = document(title, wrapper(table()), para('作业示例'))
    tree['summary'] = '作业模板 子作业参数表'
    assert classify_task_objects(tree) == tree


def test_only_types_change_and_mirrors_existing_tasks_are_consistent():
    outer = table(1)
    nested = table('1.nested', content='| 作业 | 说明 |\n| --- | --- |\n| NOP | 开始 |\n')
    nested['parent_id'] = '1.cell'
    outer['children'][0]['children'].append(nested)
    tree = document(wrapper(outer), wrapper(dict(table(2), type='task')))
    tree['mirror'] = deepcopy(nested)
    before = deepcopy(tree)
    actual = classify_task_objects(tree)
    assert tree == before and actual is not tree
    nested_copies = [o for o in all_records(actual) if o['id'] == '1.nested']
    assert len(nested_copies) >= 3
    assert all(o['type'] == 'task' for o in nested_copies)
    assert actual['blocks'][0]['object']['type'] == 'table'
    assert actual['blocks'][1]['object']['type'] == 'task'
    assert [r['id'] for r in task_object_records(tree)] == ['1.nested', 2]
    assert classify_task_objects(actual) == actual
    for obj in all_records(actual):
        if obj['type'] == 'task' and obj['id'] == '1.nested':
            obj['type'] = 'table'
    assert actual == before


def test_document_identity_prevents_cross_source_id_collisions():
    first = table(1, caption='子作业参数表')
    second = dict(table(1), source_file='different/manual.md')
    tree = document(wrapper(first), wrapper(second))
    actual = classify_task_objects(tree)
    assert [n['object']['type'] for n in actual['blocks']] == ['task', 'table']


def test_existing_task_updates_its_stale_table_mirror():
    obj = table(1)
    tree = document(wrapper(dict(obj, type='task')))
    tree['mirror'] = deepcopy(obj)
    actual = classify_task_objects(tree)
    assert actual['mirror']['type'] == 'task'
    assert task_object_records(tree)[0]['reasons'] == ()


def test_multiple_reasons_and_no_added_diagnostic_fields():
    obj = table(content='| 作业 |\n| --- |\n| NOP |\n', caption='子作业参数表')
    tree = document(section('作业示例', [wrapper(obj)]))
    assert task_object_records(tree)[0]['reasons'] == ('section_title', 'header_row', 'preceding_title')
    actual = classify_task_objects(tree)
    assert set(actual) == set(tree)
    assert set(actual['blocks'][0]['c'][1]['c'][0]['object']) == set(obj)


def test_empty_document_and_malformed_input():
    assert classify_task_objects(document()) == document()
    with pytest.raises(TypeError, match='dictionary'):
        classify_task_objects([])
    obj = table()
    del obj['id']
    with pytest.raises(ValueError, match='must have an id'):
        classify_task_objects(document({'t': 'Div', 'object': obj}))


def test_import_does_not_load_summarization_clients():
    result = subprocess.run([sys.executable, '-c',
        'import sys; import ribosome.core.dom.summary.task; '
        'assert "ribosome.core.dom.summary.object" not in sys.modules; '
        'assert "ribosome.core.dom.utils" not in sys.modules; '
        'assert "ollama" not in sys.modules; assert "openai" not in sys.modules'],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
