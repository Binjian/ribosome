import asyncio
import json
from ribosome.core.dom.summary import object as sut


def test_cell_lists_and_images_are_recursive_and_summarized(tmp_path, monkeypatch):
    path = tmp_path / 'cells.md'
    path.write_text('<table><tr><td><ul><li>Parent<img src="a.png" alt="A">'
                    '<ol start="3"><li>Child</li></ol></li></ul></td></tr></table>')
    table, = sut.extract_markdown_objects(path)
    row, = table['children']
    cell, = row['children']
    assert cell is row['cells'][0]
    listing, = cell['children']
    item, = listing['children']
    figure, nested = item['children']
    assert nested['children'][0]['content'] == 'Child'
    assert figure['images'][0]['resolved_src'] == str(tmp_path / 'a.png')
    calls = []
    async def image(**kwargs):
        return 'image description'
    async def text(**kwargs):
        payload = json.loads(kwargs['content'].split('\n', 1)[1])
        assert all(payload.get('child_summaries', ['ok']))
        calls.append(payload)
        return 'summary'
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    monkeypatch.setattr(sut, 'get_image_summary_async', image)
    asyncio.run(sut._summarize_markdown_object_async(table, None, 'test', 'en'))
    assert figure['summary'] == cell['summary'] == table['summary'] == 'summary'
    assert any(p.get('image_descriptions', [{}])[0].get('description') == 'image description' for p in calls)
    before = len(calls)
    nested['children'][0]['summary'] = ''
    asyncio.run(sut._summarize_markdown_object_async(table, None, 'test', 'en', reuse_existing=True))
    assert len(calls) == before + 1
    wrappers = sut._row_summary_nodes(table)
    assert wrappers[0]['subnodes'][0]['object'] is cell


def test_pipe_cell_markdown_reference_and_html_images(tmp_path):
    path = tmp_path / 'cells.md'
    path.write_text('| A |\n|---|\n| ![chart][fig] <img src="b.png"> |\n\n[fig]: a.png\n')
    table, = sut.extract_markdown_objects(path)
    cell = table['children'][1]['cells'][0]
    assert {n['images'][0]['src'] for n in cell['children']} == {'a.png', 'b.png'}
