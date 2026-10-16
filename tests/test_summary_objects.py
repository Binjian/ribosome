"""Offline extraction and provider-boundary checks for Markdown objects."""
import asyncio
from collections import Counter
from pathlib import Path

import pytest
from ribosome.core.dom.summary import object as sut


def document(tmp_path, text):
    file = tmp_path / 'manual.md'
    file.write_text(text, encoding='utf-8')
    return file


def test_captions_grouping_and_source_spans(tmp_path):
    text = ('**![first](img/a.png)**\n\n![second](img/b.png)\n'
            '**图 1 组合图**\n\n前面的说明\n**表 1 数据**\n\n'
            '<table><tr><td rowspan="2"><img src="inner.png"/><ul><li>x</li></ul>'
            '<table><tr><td>nested</td></tr></table></td></tr></table>\n\n'
            '说明文字\n**步骤：**\n\n1. 第一\n   - 子项\n2. 第二\n\n'
            'Options:\n\n- A\n- B\n')
    file = document(tmp_path, text)
    before = file.read_bytes()
    objects = sut.extract_markdown_objects(file)
    assert [o['type'] for o in objects] == ['figure', 'table', 'list', 'list']
    assert [o['caption'] for o in objects] == ['图 1 组合图', '表 1 数据', '步骤：', 'Options:']
    assert [i['alt'] for i in objects[0]['images']] == ['first', 'second']
    assert objects[0]['images'][0]['resolved_src'] == str(tmp_path / 'img/a.png')
    assert objects[0]['line_start'] == 1 and objects[0]['line_end'] == 4
    assert objects[1]['line_start'] == 7
    assert objects[1]['content'].startswith('<table>')
    assert 'rowspan="2"' in objects[1]['content']
    assert objects[1]['images'] == []
    assert '子项' in objects[2]['content']
    assert '说明文字' not in objects[2]['caption']
    assert [o['id'] for o in objects] == [1, 2, 3, 4]
    assert all(o['summary'] == '' for o in objects)
    assert file.read_bytes() == before


@pytest.mark.parametrize('separator', ['# Heading\n\n', 'intervening prose\n\n', '```\ncode\n```\n\n'])
def test_caption_does_not_cross_content(tmp_path, separator):
    file = document(tmp_path, '![a](a.png)\n\n' + separator + '图 1 no\n\n'
                    '表 1 no\n\n' + separator + '<table><tr><td>x</td></tr></table>\n\n'
                    'Steps:\n\n' + separator + '- item\n')
    objects = sut.extract_markdown_objects(file)
    assert [o['caption'] for o in objects] == ['', '', '']


def test_code_fences_and_captionless_objects(tmp_path):
    file = document(tmp_path, '```md\n![fake](fake.png)\n<table></table>\n- fake\n```\n\n'
                    '![real](real.png)\n\nProse\n\n<table>\n<tr><td>x</td></tr>\n</table>\n\n- real\n')
    objects = sut.extract_markdown_objects(file)
    assert [o['type'] for o in objects] == ['figure', 'table', 'list']
    assert all(o['caption'] == '' for o in objects)
    assert objects[1]['content'] == '<table>\n<tr><td>x</td></tr>\n</table>'


def test_summaries_are_atomic_captioned_and_ordered(tmp_path, monkeypatch):
    file = document(tmp_path, '![a](a.png)\n![b](b.png)\n图 1 pair\n\n表 1 data\n\n'
                    '<table><tr><td><img src="inner.png"/>x</td></tr></table>\n\nSteps:\n\n- one\n- two\n')
    visuals, prompts = [], []
    async def image(**kwargs):
        visuals.append(kwargs['image_link'])
        await asyncio.sleep(0)
        return 'visual:' + Path(kwargs['image_link']).name
    async def text(**kwargs):
        prompts.append(kwargs['content'])
        await asyncio.sleep(0)
        return 'summary:' + kwargs['content']
    monkeypatch.setattr(sut, 'get_image_summary_async', image)
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    results = asyncio.run(sut.summarize_markdown_objects_async(file, object(), 'offline'))
    assert [o['id'] for o in results] == [1, 2, 3]
    assert len(visuals) == 2 and len(prompts) == 3
    assert all(Path(p).parent == tmp_path for p in visuals)
    assert all(x in results[0]['summary'] for x in ['图 1 pair', 'visual:a.png', 'visual:b.png'])
    assert '<table>' in results[1]['summary'] and '表 1 data' in results[1]['summary']
    assert '- one\\n- two' in results[2]['summary'] and 'Steps:' in results[2]['summary']


def test_failure_cleans_up_siblings_and_locates_object(tmp_path, monkeypatch):
    file = document(tmp_path, '- one\n\n<table><tr><td>x</td></tr></table>\n')
    active = set()
    async def fail(**kwargs):
        key = kwargs['content']
        active.add(key)
        try:
            if 'HTML table' in key:
                await asyncio.sleep(0)
                raise RuntimeError('provider failed')
            await asyncio.sleep(20)
        finally:
            active.remove(key)
    monkeypatch.setattr(sut, 'get_summary_response_async', fail)
    with pytest.raises(ValueError, match=r'table object 2 at .*manual.md:3-3: provider failed'):
        asyncio.run(sut.summarize_markdown_objects_async(file, object(), 'offline'))
    assert not active


def test_cancellation_cleans_up_requests(tmp_path, monkeypatch):
    file = document(tmp_path, '- item\n')
    started, finished = [], []
    async def slow(**kwargs):
        started.append(True)
        try:
            await asyncio.sleep(20)
        finally:
            finished.append(True)
    monkeypatch.setattr(sut, 'get_summary_response_async', slow)
    async def scenario():
        task = asyncio.create_task(sut.summarize_markdown_objects_async(file, object(), 'offline'))
        while not started:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert finished == [True]


def test_supplied_manual_extraction():
    file = Path('assets/SX322017-C-1/SX322017《新松机器人跟随应用操作手册》(C-1).md')
    if not file.exists():
        pytest.skip('Local manual asset is unavailable')
    before = file.read_bytes()
    objects = sut.extract_markdown_objects(file)
    assert Counter(o['type'] for o in objects)['table'] == 15
    assert any(o['type'] == 'figure' and o['caption'] == '图 1-1 直线传送带跟踪系统' for o in objects)
    assert any(o['type'] == 'figure' and len(o['images']) == 2 and o['caption'] == '图 2-2 IO设备列表' for o in objects)
    assert any(o['type'] == 'table' and o['caption'].startswith('表 1-1') for o in objects)
    assert any(o['type'] == 'list' and o['caption'] == '界面数据项释义：' for o in objects)
    assert file.read_bytes() == before


def test_reference_images_and_empty_file(tmp_path):
    file = document(tmp_path, '![label][image]\n\n图 1 reference\n\n[image]: img/a.png\n')
    result = sut.extract_markdown_objects(file)
    assert result[0]['images'][0]['src'] == 'img/a.png'
    assert result[0]['caption'] == '图 1 reference'
    file.write_text('', encoding='utf-8')
    assert sut.extract_markdown_objects(file) == []


def test_object_requests_respect_concurrency_limit(tmp_path, monkeypatch):
    file = document(tmp_path, '\n\n'.join('<table><tr><td>x</td></tr></table>' for _ in range(10)))
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
    monkeypatch.setattr(sut, '_SUMMARY_CONCURRENCY', 2)
    monkeypatch.setattr(sut, '_SUMMARY_TASK_LIMIT', 3)
    monkeypatch.setattr(sut, 'get_summary_response_async', text)
    results = asyncio.run(sut.summarize_markdown_objects_async(file, object(), 'offline'))
    assert len(results) == 10 and peak == 2 and active == 0


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel'])
def test_progress_tracks_completed_objects_and_always_closes(tmp_path, monkeypatch, outcome):
    file = document(tmp_path, '- item\n')
    bars = []
    class Bar:
        def __init__(self, **kwargs):
            self.total = kwargs['total']
            self.completed = 0
            self.closed = False
            bars.append(self)
        def update(self, amount):
            self.completed += amount
        def close(self):
            self.closed = True
    async def summarize(**kwargs):
        if outcome == 'failure':
            raise RuntimeError('failed')
        if outcome == 'cancel':
            raise asyncio.CancelledError()
        return 'summary'
    monkeypatch.setattr('tqdm.auto.tqdm', Bar)
    monkeypatch.setattr(sut, 'get_summary_response_async', summarize)
    async def run():
        return await sut.summarize_markdown_objects_async(file, object(), 'offline', show_progress=True)
    if outcome == 'success':
        asyncio.run(run())
    else:
        with pytest.raises(ValueError if outcome == 'failure' else asyncio.CancelledError):
            asyncio.run(run())
    assert bars[0].total == 1
    assert bars[0].completed == (1 if outcome == 'success' else 0)
    assert bars[0].closed


def test_manual_ollama_configuration_uses_project_env(tmp_path, monkeypatch):
    (tmp_path / 'pyproject.toml').write_text('')
    (tmp_path / '.env').write_text('OLLAMA_HOST=http://configured:11434\nOLLAMA_SUMMARY_MODEL=vision-model\nOLLAMA_API_KEY=test-key\n')
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path))
    monkeypatch.setenv('OLLAMA_HOST', 'http://stale:11434')
    captured = {}
    def client(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(sut, 'AsyncClient', client)
    _, model = sut._configured_ollama_summary_client()
    assert model == 'vision-model'
    assert captured == {'host': 'http://configured:11434', 'headers': {'Authorization': 'Bearer test-key'}}
    (tmp_path / '.env').write_text('OLLAMA_HOST=http://configured:11434\n')
    with pytest.raises(ValueError, match='OLLAMA_SUMMARY_MODEL'):
        sut._configured_ollama_summary_client()
    (tmp_path / '.env').unlink()
    with pytest.raises(FileNotFoundError, match='Missing configuration'):
        sut._configured_ollama_summary_client()


def test_sx322018_notebook_function_uses_configured_root_and_exports(tmp_path, monkeypatch):
    import json
    import os
    notebook = json.loads(Path('nbs/01.core.dom.summary.object.ipynb').read_text())
    source = next(''.join(c['source']) for c in notebook['cells']
                  if 'async def test_summarize_objects_with_sx322018_md' in ''.join(c['source']))
    (tmp_path / 'pyproject.toml').write_text('')
    file = tmp_path / 'assets/SX322018-A-0/SX322018《新松机器人气动点焊应用操作手册》-A-0.md'
    file.parent.mkdir(parents=True)
    file.write_text('unchanged source', encoding='utf-8')
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path))
    records = [{'id': i, 'type': kind, 'summary': ''}
               for i, kind in enumerate(['figure', 'table', 'list'], 1)]
    client = object()
    async def summarize(path, actual_client, model, **kwargs):
        assert path == file and actual_client is client and model == 'configured-model'
        assert kwargs == {'lang': 'zh', 'show_progress': True}
        return {'blocks': [{'t': 'Section', 'c': [
            {'t': 'Div', 'c': [[], []], 's': '摘要',
             'object': {**record, 'summary': '摘要'}} for record in records],
            'subnodes': []}], 'summary': '文档摘要'}
    def eq(a, b):
        assert a == b
    scope = {'Path': Path, 'os': os, 'json': json, 'test_eq': eq,
             'extract_markdown_objects': lambda path: records,
             '_configured_ollama_summary_client': lambda: (client, 'configured-model'),
             'summarize_markdown_object_tree_async': summarize,
             '_object_nodes': sut._object_nodes,
             '_write_semantics_tree': sut._write_semantics_tree}
    exec(compile(source, '<sx322018-notebook-test>', 'exec'), scope)
    result = asyncio.run(scope['test_summarize_objects_with_sx322018_md']())
    output = file.with_name(file.stem + '_semantics.json')
    assert json.loads(output.read_text(encoding='utf-8')) == result
    assert file.read_text(encoding='utf-8') == 'unchanged source'
