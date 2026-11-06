"""Offline regressions for whole-document resume and publication boundaries."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

from ribosome.core.dom.summary import object as dom
from utils.summary_asyncio import SummaryCheckpoint
from utils.summary_object_asyncio import summarize_object_documents


class QuietProgress:
    def __init__(self, total=0, **kwargs): self.total, self.n = total, 0
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def refresh(self): pass
    def set_postfix(self, *args, **kwargs): pass
    def set_postfix_str(self, *args, **kwargs): pass
    @staticmethod
    def write(message): pass


def nodes(value):
    if isinstance(value, dict):
        yield value
        if 'object' in value:
            return
        for key in ('blocks', 'c', 'subnodes'):
            yield from nodes(value.get(key, []))
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


@pytest.mark.parametrize('kind', ['document', 'section', 'prose'])
def test_completed_output_repairs_nonobject_gaps_only(tmp_path, monkeypatch, kind):
    file = tmp_path / 'manual.md'
    file.write_text('# Manual\n\nFirst paragraph describes ALARM42.\n\nSecond paragraph gives limits.\n\n- first\n- second\n')
    calls = []
    async def text(**kwargs):
        calls.append(kwargs['content'])
        return f'Summary-{len(calls)}'
    monkeypatch.setattr(dom, 'get_summary_response_async', text)
    checkpoint = tmp_path / 'checkpoint.json'
    async def run():
        return await summarize_object_documents([file], object(), 'fake', checkpoint_path=checkpoint,
                                               leaf_min_len=1, progress_factory=QuietProgress)
    assert asyncio.run(run())[0]['status'] == 'completed'
    output = file.with_name('manual_semantics.json')
    tree = json.loads(output.read_text())
    chosen = tree if kind == 'document' else next(node for node in nodes(tree)
        if node.get('t') == ('Section' if kind == 'section' else 'Str') and node.get('s'))
    chosen.pop('summary' if kind == 'document' else 's')
    previous = deepcopy(tree)
    output.write_text(json.dumps(tree))
    saved = json.loads(checkpoint.read_text())
    saved['entries'][str(file)]['signature'] = SummaryCheckpoint.signature(file)
    checkpoint.write_text(json.dumps(saved))
    calls.clear()
    row, = asyncio.run(run())
    assert row['status'] == 'completed' and row['incremental']
    upgraded = json.loads(output.read_text())
    assert dom.audit_completeness(upgraded)['complete']
    def compare(old, new):
        if isinstance(old, dict):
            for field in ('s', 'summary'):
                if field in old:
                    assert new[field] == old[field]
            for key, value in old.items():
                if key in new:
                    compare(value, new[key])
        elif isinstance(old, list):
            for a, b in zip(old, new): compare(a, b)
    compare(previous, upgraded)
    assert len(calls) <= 1
    before = output.read_bytes()
    calls.clear()
    assert asyncio.run(run())[0]['resumed']
    assert not calls and output.read_bytes() == before


def test_audit_distinguishes_ignored_skipped_and_missing_without_mutation():
    tree = {'blocks': [
        {'t': 'Para', 's': '', 'summary_skipped': True},
        {'t': 'Div', 'object': {'id': 1, 'type': 'figure', 'summary': '', 'summary_ignored': True}},
        {'t': 'Section', 's': '', 'subnodes': [{'t': 'Str', 'c': 'value', 's': 'value'}]},
    ], 'summary': 'document'}
    before = deepcopy(tree)
    report = dom.audit_completeness(tree)
    assert tree == before
    assert {item['status'] for item in report['items']} == {
        'present', 'missing', 'intentionally_ignored', 'intentionally_skipped'}
    assert len(report['missing']) == 1 and report['missing'][0]['kind'] == 'sections'


def test_nested_list_figure_and_blockquote_table_are_typed_and_visual(tmp_path, monkeypatch):
    file = tmp_path / 'manual.md'
    file.write_text('- Before ![diagram](device.png) after\n\n'
                    '  ```text\n  SET A, 2\n  ```\n\n'
                    '> | Name | Value |\n> | --- | --- |\n> | ALARM42 | 2 |\n')
    records = dom.extract_markdown_objects(file)
    listed = next(record for record in records if record['type'] == 'list')
    kinds = [child['type'] for child in listed['children'][0]['children']]
    assert kinds == ['figure', 'code']
    table = next(record for record in records if record['type'] == 'table')
    assert len(table['children']) == 2 and 'ALARM42' in table['content']
    images = []
    async def image(**kwargs):
        images.append(kwargs['image_link'])
        return 'visual evidence'
    async def text(**kwargs): return 'description'
    monkeypatch.setattr(dom, 'get_image_summary_async', image)
    monkeypatch.setattr(dom, 'get_summary_response_async', text)
    asyncio.run(dom.summarize_markdown_objects_async(file, object(), 'fake'))
    assert images == [str(tmp_path / 'device.png')]


def test_inline_prose_image_and_formula_retain_source(tmp_path, monkeypatch):
    file = tmp_path / 'manual.md'
    source = 'Check ![device](device.png) before startup.\n\n$$x = 42$$\n'
    file.write_text(source)
    records = dom.extract_markdown_objects(file)
    assert [record['type'] for record in records] == ['paragraph', 'formula']
    assert records[0]['children'][0]['type'] == 'figure'
    assert records[0]['content'] == source.split('\n\n')[0] + '\n'
    assert records[1]['content'] == '$$x = 42$$\n'
    visuals = []
    async def image(**kwargs):
        visuals.append(kwargs['image_link'])
        return 'diagram description'
    async def text(**kwargs): return 'summary'
    monkeypatch.setattr(dom, 'get_image_summary_async', image)
    monkeypatch.setattr(dom, 'get_summary_response_async', text)
    tree = asyncio.run(dom.summarize_markdown_object_tree_async(file, object(), 'fake'))
    assert dom.audit_completeness(tree)['complete']
    assert visuals == [str(tmp_path / 'device.png')]
    assert file.read_text() == source


def test_asset_adoption_preserves_legacy_then_detects_byte_changes(tmp_path):
    source = tmp_path / 'manual.md'
    source.write_text('![diagram](diagram.png)\n![remote](https://example.invalid/figure.png)\n')
    (tmp_path / 'diagram.png').write_bytes(b'first')
    source.with_name('manual_semantics.json').write_text('{"summary":"saved"}')
    cp = SummaryCheckpoint(tmp_path / 'checkpoint.json', 'fake', None, True)
    cp.record(source, {'status': 'completed'})
    cp.entries[str(source)].pop('assets')
    cp.entries[str(source)].pop('asset_provenance')
    cp.save()
    signature = cp.signature(source)
    assert cp.completed(source)['resumed']
    assert cp.entries[str(source)]['asset_provenance'] == 'historical_unknown'
    assert cp.entries[str(source)]['assets']['https://example.invalid/figure.png']['state'] == 'remote_unverified'
    (tmp_path / 'diagram.png').write_bytes(b'second')
    assert cp.signature(source) == signature
    assert cp.completed(source) is None


def test_committed_summary_reconciles_checkpoint_after_restart(tmp_path, monkeypatch):
    source = tmp_path / 'manual.md'
    source.write_text('source')
    output = source.with_name('manual_semantics.json')
    output.write_text('{"summary":"previous"}')
    cp = SummaryCheckpoint(tmp_path / 'checkpoint.json', 'fake', None, True)
    cp.record(source, {'status': 'completed'})
    previous_checkpoint = cp.path.read_bytes()
    tree = {'blocks': [{'t': 'Str', 'c': 'source', 's': 'saved'}], 'summary': 'upgraded'}
    def interrupted(): raise OSError('crash after output projection')
    monkeypatch.setattr(cp, 'save', interrupted)
    row = {'status': 'completed'}
    cp.publish(source, tree, row, dom._write_semantics_tree)
    assert row['publication_pending'] and cp.path.read_bytes() == previous_checkpoint
    restarted = SummaryCheckpoint(cp.path, 'fake', None, True)
    assert restarted.completed(source)['resumed']
    assert json.loads(output.read_text()) == tree
    assert not restarted._publication_journal(source).exists()


def test_summary_preparation_failure_preserves_old_output_and_checkpoint(tmp_path):
    source = tmp_path / 'manual.md'
    source.write_text('source')
    output = source.with_name('manual_semantics.json')
    output.write_text('previous')
    cp = SummaryCheckpoint(tmp_path / 'checkpoint.json', 'fake', None, True)
    cp.record(source, {'status': 'completed'})
    before = cp.path.read_bytes()
    def fail(*args): raise OSError('staging failed')
    with pytest.raises(OSError, match='staging failed'):
        cp.publish(source, {'blocks': [], 'summary': 'new'}, {'status': 'completed'}, fail)
    assert output.read_text() == 'previous' and cp.path.read_bytes() == before


@pytest.mark.parametrize('boundary', ['prepared', 'checkpoint'])
def test_process_death_reconciles_prepared_and_committed_publications(tmp_path, boundary):
    source = tmp_path / 'manual.md'
    source.write_text('source')
    output = source.with_name('manual_semantics.json')
    output.write_text('{"summary":"previous"}')
    cp = SummaryCheckpoint(tmp_path / 'checkpoint.json', 'fake', None, True)
    cp.record(source, {'status': 'completed'})
    previous = output.read_bytes()
    program = r'''
import json, os, sys
from pathlib import Path
from utils.summary_asyncio import SummaryCheckpoint
source, checkpoint, boundary = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
cp = SummaryCheckpoint(checkpoint, 'fake', None, True)
if boundary == 'checkpoint':
    cp.save = lambda: os._exit(73)
else:
    atomic = cp._atomic_json
    def interrupt(path, record):
        if record.get('state') == 'committed': os._exit(73)
        atomic(path, record)
    cp._atomic_json = interrupt
tree = {'blocks': [], 'summary': 'prepared complete summary'}
cp.publish(source, tree, {'status': 'completed'}, lambda path, value: path.write_text(json.dumps(value)))
'''
    result = subprocess.run([sys.executable, '-c', program, str(source), str(cp.path), boundary],
                            env=dict(os.environ), capture_output=True, text=True, timeout=20)
    assert result.returncode == 73, result.stderr
    if boundary == 'prepared':
        assert output.read_bytes() == previous
    restarted = SummaryCheckpoint(cp.path, 'fake', None, True)
    assert restarted.completed(source)['resumed']
    assert json.loads(output.read_text())['summary'] == 'prepared complete summary'
    assert not restarted._publication_journal(source).exists()


def test_summary_budget_splits_units_and_rejects_one_oversized_unit(monkeypatch):
    seen = []
    async def text(**kwargs):
        seen.append(kwargs['content'])
        return 'short'
    monkeypatch.setattr(dom, 'get_summary_response_async', text)
    async def run():
        token = dom._summary_runtime.set(dom.SummaryRuntime(input_token_budget=256))
        try:
            result = await dom._request_summary_response_async(object(), ('fact A 42 mm\n' * 30), model='fake')
            assert result == 'short' and len(seen) > 1
            assert all(len(value.encode()) <= 192 for value in seen)
            with pytest.raises(ValueError, match='semantic summary unit'):
                await dom._request_summary_response_async(object(), 'x' * 300, model='fake')
        finally:
            dom._summary_runtime.reset(token)
    asyncio.run(run())


def test_new_code_object_preserves_legacy_ordinals_and_saved_summaries(tmp_path, monkeypatch):
    file = tmp_path / 'manual.md'
    file.write_text('# Manual\n\n```text\nSET A, 42\n```\n\n- first\n- second\n')
    extract = dom.extract_markdown_object_tree
    legacy = extract(file)
    def old_projection(value):
        if isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, dict) and child.get('object', {}).get('type') == 'code':
                    value[index] = child['c'][1][0]
                else:
                    old_projection(child)
        elif isinstance(value, dict):
            for key in ('blocks', 'c', 'subnodes'):
                old_projection(value.get(key, []))
    old_projection(legacy)
    def renumber(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {'id', 'parent_id', 'list_id'}:
                    if child == 2: value[key] = 1
                    elif isinstance(child, str) and child.startswith('2.'): value[key] = '1.' + child[2:]
                else:
                    renumber(child)
        elif isinstance(value, list):
            for child in value: renumber(child)
    renumber(legacy)
    calls = []
    async def text(**kwargs):
        calls.append(kwargs['content'])
        return f'saved-{len(calls)}'
    monkeypatch.setattr(dom, 'get_summary_response_async', text)
    monkeypatch.setattr(dom, 'extract_markdown_object_tree', lambda path: deepcopy(legacy))
    async def run():
        return await summarize_object_documents([file], object(), 'fake',
            checkpoint_path=tmp_path / 'checkpoint.json', leaf_min_len=1, progress_factory=QuietProgress)
    assert asyncio.run(run())[0]['status'] == 'completed'
    output = file.with_name('manual_semantics.json')
    saved = json.loads(output.read_text())
    saved_list = next(dom._object_nodes(saved))['object']
    monkeypatch.setattr(dom, 'extract_markdown_object_tree', extract)
    calls.clear()
    result, = asyncio.run(run())
    assert result['status'] == 'completed', result
    upgraded = json.loads(output.read_text())
    upgraded_list = next(node['object'] for node in dom._object_nodes(upgraded) if node['object']['type'] == 'list')
    assert upgraded_list['id'] == saved_list['id'] == 1
    assert upgraded_list['summary'] == saved_list['summary']
    assert upgraded['summary'] == saved['summary'] and not calls
    before = output.read_bytes()
    assert asyncio.run(run())[0]['resumed']
    assert output.read_bytes() == before


def test_canonical_projection_failure_retries_without_model_requests(tmp_path, monkeypatch):
    from ribosome.core.artifacts import ArtifactRepository
    repository = ArtifactRepository(tmp_path / 'artifacts')
    file = tmp_path / 'manual.md'
    file.write_text('# Manual\n\n- first\n- second\n')
    calls = []
    async def text(**kwargs):
        calls.append(kwargs['content'])
        return 'saved source description'
    monkeypatch.setattr(dom, 'get_summary_response_async', text)
    commit = repository.commit
    def interrupt(*args, **kwargs): raise OSError('publication pointer unavailable')
    monkeypatch.setattr(repository, 'commit', interrupt)
    async def run():
        return await summarize_object_documents([file], object(), 'fake',
            artifact_repository=repository, leaf_min_len=1, progress_factory=QuietProgress)
    first, = asyncio.run(run())
    assert first['status'] == 'completed' and first['publication_pending']
    before = file.with_name('manual_semantics.json').read_bytes()
    calls.clear()
    monkeypatch.setattr(repository, 'commit', commit)
    second, = asyncio.run(run())
    assert second['resumed'] and not calls
    assert file.with_name('manual_semantics.json').read_bytes() == before
    document = repository.adopt_document(str(file))
    assert repository.read(document.id).document.id == document.id


def test_independent_batches_share_runtime_safely_and_reject_overlapping_writers(tmp_path, monkeypatch):
    files = [tmp_path / f'{name}.md' for name in ('one', 'two')]
    for file in files:
        file.write_text('- first\n- second\n')
    original = dom._summarize_markdown_object_async
    async def scenario():
        release = asyncio.Event()
        started = asyncio.Event()
        calls = 0
        async def text(**kwargs):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return 'saved'
        monkeypatch.setattr(dom, 'get_summary_response_async', text)
        async def run(file, checkpoint_path=None):
            return await summarize_object_documents([file], object(), 'fake',
                checkpoint_path=checkpoint_path or file.with_suffix('.checkpoint'), progress_factory=QuietProgress)
        tasks = [asyncio.create_task(run(file)) for file in files]
        try:
            await asyncio.wait_for(started.wait(), 2)
            with pytest.raises(RuntimeError, match='active writer'):
                await run(files[0])
            other = tmp_path / 'other.md'
            other.write_text('- unrelated source\n')
            with pytest.raises(RuntimeError, match='active writer'):
                await run(other, files[0].with_suffix('.checkpoint'))
            assert dom._summarize_markdown_object_async is original
        finally:
            release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        assert all(rows[0]['status'] == 'completed' for rows in results)
        assert calls > 0 and dom._summary_runtime.get() is None
    asyncio.run(scenario())
