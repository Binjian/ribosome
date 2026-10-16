"""Recursive object DOM acceptance tests with offline providers."""
import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from ribosome.core.dom.summary import object as sut


def write(tmp_path, source):
    file = tmp_path / 'manual.md'
    file.write_text(source, encoding='utf-8')
    return file


def nodes(value):
    if isinstance(value, dict):
        yield value
        for key, child in value.items():
            if key != 'object':
                yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def sample(tmp_path):
    return write(tmp_path, '# Parent\n\nOpening prose.\n\n## Child\n\n'
                 '![a][image]\n![b](b.png)\n图 1 pair\n\n'
                 '表 1 data\n\n<table><tr><td rowspan="2">x'
                 '<table><tr><td>inner</td></tr></table></td></tr></table>\n\n'
                 'Steps:\n\n1. first\n   - nested\n2. second\n\n'
                 '### Grandchild\n\nClosing prose.\n\n[image]: img/a.png\n')


def test_extraction_preserves_sections_objects_and_reference_images(tmp_path):
    file = sample(tmp_path)
    before = file.read_bytes()
    tree = sut.extract_markdown_object_tree(file)
    wrappers = list(sut._object_nodes(tree))
    assert [n['object'] for n in wrappers] == sut.extract_markdown_objects(file)
    assert [n['object']['type'] for n in wrappers] == ['figure', 'table', 'list']
    assert [n['object']['caption'] for n in wrappers] == ['图 1 pair', '表 1 data', 'Steps:']
    sections = [n for n in nodes(tree) if n.get('t') == 'Section']
    assert [n['c'][0]['c'][0] for n in sections] == [1, 2, 3]
    assert sections[0]['subnodes'][0] is sections[1]
    assert sections[1]['subnodes'][0] is sections[2]
    assert list(sut._object_nodes(sections[1]['c'][1])) == wrappers
    assert any(n.get('t') == 'Str' and n.get('c') == 'Opening' for n in nodes(tree))
    assert any(n.get('t') == 'Str' and n.get('c') == 'Closing' for n in nodes(tree))
    images = [n for n in nodes(wrappers[0]) if n.get('t') == 'Image']
    assert [n['c'][2][0] for n in images] == ['img/a.png', 'b.png']
    assert any(n.get('t') == 'BulletList' for n in nodes(wrappers[2]))
    assert tree['title'] == 'manual' and tree['file_path'] == str(file.resolve())
    assert file.read_bytes() == before
    assert list(tmp_path.iterdir()) == [file]
    assert sut.extract_markdown_object_tree(file) == tree


def test_same_line_tables_and_surrounding_html_are_distinct(tmp_path):
    file = write(tmp_path, '# Title\n\n<div>before</div><table><tr><td>a</td></tr></table>'
                 '<table><tr><td>b</td></tr></table><div>after</div>\n')
    tree = sut.extract_markdown_object_tree(file)
    wrappers = list(sut._object_nodes(tree))
    assert len(wrappers) == 2
    assert ['<td>a</td>' in wrappers[0]['object']['content'],
            '<td>b</td>' in wrappers[1]['object']['content']] == [True, True]
    assert wrappers[0]['object']['line_start'] == wrappers[1]['object']['line_start'] == 3
    assert any(n.get('t') == 'Str' and n.get('c') == 'before' for n in nodes(tree))
    assert any(n.get('t') == 'Str' and n.get('c') == 'after' for n in nodes(tree))


def test_summaries_are_atomic_and_propagate_to_all_ancestors(tmp_path, monkeypatch):
    file = sample(tmp_path)
    original = sut.extract_markdown_object_tree(file)
    calls, images = [], []
    async def text(client, content, **kwargs):
        calls.append(content)
        if content.startswith('Summarize this'):
            return 'OBJECT-' + str(len([x for x in calls if x.startswith('Summarize this')]))
        return content
    async def image(**kwargs):
        images.append(kwargs['image_link'])
        return 'VISUAL'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    monkeypatch.setattr(sut, 'get_image_summary_async', image)
    result = asyncio.run(sut.summarize_markdown_object_tree_async(file, object(), 'offline', leaf_min_len=1))
    wrappers = list(sut._object_nodes(result))
    assert len([x for x in calls if x.startswith('Summarize this')]) == 3
    assert len(images) == 2
    for before, after in zip(sut._object_nodes(original), wrappers):
        assert before['c'] == after['c']
        assert after['s'] == after['object']['summary']
        assert after['s'] in result['summary']
        assert after['s'] in result['blocks'][0]['s']
        assert after['s'] in result['blocks'][0]['subnodes'][0]['s']
    assert 'Opening' in result['summary'] and 'Closing' in result['summary']
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel'])
def test_tree_cleanup_progress_and_atomic_publication(tmp_path, monkeypatch, outcome):
    file = write(tmp_path, '# Title\n\n' + '\n\n'.join('<table><tr><td>x</td></tr></table>' for _ in range(4)))
    destination = tmp_path / 'manual_semantics.json'
    destination.write_text('previous')
    active, bars = set(), []
    class Bar:
        def __init__(self, **kwargs):
            self.total, self.completed, self.closed = kwargs['total'], 0, False
            bars.append(self)
        def update(self, n): self.completed += n
        def close(self): self.closed = True
    async def text(client, content, **kwargs):
        identity = object()
        active.add(identity)
        try:
            await asyncio.sleep(0)
            if outcome == 'failure': raise RuntimeError('provider failed')
            if outcome == 'cancel': raise asyncio.CancelledError()
            return 'summary'
        finally:
            active.remove(identity)
    monkeypatch.setattr('tqdm.auto.tqdm', Bar)
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    async def run():
        tree = await sut.summarize_markdown_object_tree_async(file, object(), 'offline', show_progress=True)
        sut._write_semantics_tree(destination, tree)
        return tree
    if outcome == 'success':
        tree = asyncio.run(run())
        assert json.loads(destination.read_text()) == tree
        assert bars[0].completed == 4
    else:
        with pytest.raises(ValueError if outcome == 'failure' else asyncio.CancelledError):
            asyncio.run(run())
        assert destination.read_text() == 'previous'
    assert not active and bars[0].closed and bars[0].total == 4
    assert not list(tmp_path.glob('*.tmp'))
    assert sut._object_summary_progress.get() is None


def test_empty_tree_makes_no_requests(tmp_path, monkeypatch):
    file = write(tmp_path, '')
    async def fail(*args, **kwargs): raise AssertionError('unexpected request')
    monkeypatch.setattr(sut, 'get_summary_response_async', fail)
    result = asyncio.run(sut.summarize_markdown_object_tree_async(file, object(), 'offline'))
    assert result['blocks'] == [] and result['summary'] == ''


def test_tree_concurrency_is_bounded(tmp_path, monkeypatch):
    file = write(tmp_path, '\n\n'.join('<table><tr><td>x</td></tr></table>' for _ in range(10)))
    active = peak = 0
    async def text(client, content, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001)
            return 'summary'
        finally:
            active -= 1
    monkeypatch.setattr(sut, '_SUMMARY_CONCURRENCY', 2)
    monkeypatch.setattr(sut, '_SUMMARY_TASK_LIMIT', 3)
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    tree = asyncio.run(sut.summarize_markdown_object_tree_async(file, object(), 'offline'))
    assert len(list(sut._object_nodes(tree))) == 10
    assert peak == 2 and active == 0


def test_failed_atomic_replace_preserves_old_file(tmp_path, monkeypatch):
    destination = tmp_path / 'semantics.json'
    destination.write_text('previous')
    def fail(*args): raise OSError('replacement failed')
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError, match='replacement failed'):
        sut._write_semantics_tree(destination, {'blocks': [], 'summary': ''})
    assert destination.read_text() == 'previous'
    assert not list(tmp_path.glob('*.tmp'))


def test_supplied_manual_tree():
    file = Path('assets/SX322017-C-1/SX322017《新松机器人跟随应用操作手册》(C-1).md')
    if not file.exists(): pytest.skip('Local manual asset unavailable')
    before = file.read_bytes()
    tree = sut.extract_markdown_object_tree(file)
    wrappers = list(sut._object_nodes(tree))
    assert len(wrappers) == 74
    assert Counter(n['object']['type'] for n in wrappers)['table'] == 15
    assert [n['object'] for n in wrappers] == sut.extract_markdown_objects(file)
    sections = [n for n in nodes(tree) if n.get('t') == 'Section']
    assert Counter(n['c'][0]['c'][0] for n in sections) == {1: 10, 2: 15, 3: 16}
    assert any(n.get('subnodes') and any(c.get('subnodes') for c in n['subnodes']) for n in sections)
    assert file.read_bytes() == before


def test_empty_object_summary_reports_source_location(tmp_path, monkeypatch):
    file = write(tmp_path, '- item\n')
    async def empty(client, content, **kwargs): return ''
    monkeypatch.setattr(sut, 'get_summary_response_async', empty)
    with pytest.raises(ValueError, match=r'list object 1 at .*manual.md:1-1: Provider returned an empty'):
        asyncio.run(sut.summarize_markdown_object_tree_async(file, object(), 'offline'))


def test_pipe_tables_preserve_captions_spans_and_atomic_subtrees(tmp_path, monkeypatch):
    table = '| Item | Value |\n| :--- | ---: |\n| first | 2 |\n| second | 3 |\n'
    file = write(tmp_path, '# Parent\n\n## Child\n\n表 1 数据\n\n' + table + '\nAfter.\n')
    before = file.read_bytes()
    extracted = sut.extract_markdown_objects(file)
    assert len(extracted) == 1
    record = extracted[0]
    assert record['type'] == 'table' and record['caption'] == '表 1 数据'
    assert record['content'] == table
    assert (record['line_start'], record['line_end']) == (5, 10)
    tree = sut.extract_markdown_object_tree(file)
    wrapper = next(sut._object_nodes(tree))
    assert any(node.get('t') == 'Table' for node in nodes(wrapper))
    prompts = []
    async def text(client, content, **kwargs):
        prompts.append(content)
        return 'PIPE-TABLE-SUMMARY'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    result = asyncio.run(sut.summarize_markdown_object_tree_async(
        file, object(), 'offline', leaf_min_len=99999))
    summarized = next(sut._object_nodes(result))
    assert summarized['c'] == wrapper['c']
    assert len(prompts) == 1 and 'complete Markdown pipe table' in prompts[0]
    assert json.loads(prompts[0].split('\n', 1)[1]) == {'caption': '表 1 数据', 'content': table}
    assert summarized['s'] in result['blocks'][0]['subnodes'][0]['s']
    assert summarized['s'] in result['summary']
    assert file.read_bytes() == before


def test_pipe_tables_ignore_code_and_stay_inside_enclosing_lists(tmp_path):
    file = write(tmp_path, '```md\n| fake |\n| --- |\n| x |\n```\n\n'
                 '- item\n\n  | nested |\n  | --- |\n  | x |\n\n'
                 '| real |\n| --- |\n| y |\n\n<table><tr><td>html</td></tr></table>\n')
    objects = sut.extract_markdown_objects(file)
    assert [o['type'] for o in objects] == ['list', 'table', 'table']
    assert '| nested |' in objects[0]['content']
    assert objects[1]['content'] == '| real |\n| --- |\n| y |\n'
    assert objects[2]['content'].startswith('<table>')
