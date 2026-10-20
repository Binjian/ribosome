"""Durable unpublished progress and its compatibility checks."""
from types import SimpleNamespace

from utils.summary_asyncio import SummaryCheckpoint
from utils.summary_object_asyncio import _load_partial, _save_partial, _complete_object


def test_partial_is_atomic_separate_and_compatible(tmp_path):
    file = tmp_path / 'manual.md'
    file.write_text('original')
    output = tmp_path / 'manual_semantics.json'
    output.write_text('previous output')
    checkpoint = SummaryCheckpoint(tmp_path / 'checkpoint.json', 'model', 'server', True,
                                   settings={'lang': 'en', 'object_schema_version': 5})
    tree = {'blocks': [{'object': {'summary': 'completed'}}], 'summary': ''}
    _save_partial(file, checkpoint, tree)
    assert _load_partial(file, checkpoint) == tree
    assert output.read_text() == 'previous output'
    assert not checkpoint.path.exists()
    checkpoint.config['host'] = 'another server'
    checkpoint.config['settings']['object_schema_version'] = 6
    assert _load_partial(file, checkpoint) == tree
    checkpoint.config['settings']['lang'] = 'zh'
    assert _load_partial(file, checkpoint) is None
    checkpoint.config['settings']['lang'] = 'en'
    file.write_text('changed source')
    assert _load_partial(file, checkpoint) is None
    assert not list(tmp_path.glob('tmp*'))


def test_complete_object_requires_all_descendants():
    dom = SimpleNamespace(object_utils=SimpleNamespace(
        _has_object_summary=lambda obj: bool(obj.get('summary')) or obj.get('summary_ignored', False)))
    child = {'summary': ''}
    parent = {'summary': 'parent', 'children': [child]}
    assert not _complete_object(parent, dom)
    child['summary'] = 'child'
    assert _complete_object(parent, dom)
    child.update(summary='', summary_ignored=True)
    assert _complete_object(parent, dom)


def test_interrupted_document_reuses_objects_and_restores_count(tmp_path, monkeypatch):
    import asyncio
    import copy
    from ribosome.core.dom.summary import object as dom
    from utils import summary_object_asyncio as app

    file = tmp_path / 'manual.md'
    file.write_text('source')
    output = tmp_path / 'manual_semantics.json'
    output.write_text('previous output')
    original = {'blocks': [{'object': {'id': str(i), 'summary': '', 'children': []}}
                           for i in range(2)], 'summary': ''}
    original['subnodes'] = [{'t': 'Section', 'c': []}]
    calls, observed, completion_updates = [], [], []
    interrupt = True

    class Progress:
        def __init__(self, total, **kwargs): self.total, self.n = total, 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def set_postfix(self, **kwargs): pass
        def set_postfix_str(self, text, **kwargs):
            if 'manual.md: objects' in text: observed.append((self.n, self.total, text))
        def refresh(self): pass
        @staticmethod
        def write(text): pass

    async def snapshot(): return SimpleNamespace(signature='ignored-images')
    async def summarize(obj, *args, reuse_existing=False, **kwargs):
        if reuse_existing and obj['summary']: return obj
        calls.append(obj['id'])
        if interrupt and obj['id'] == '1': raise asyncio.CancelledError()
        obj['summary'] = 'summary-' + obj['id']
        return obj
    async def tree_summary(file, *args, **kwargs):
        tree = dom.extract_markdown_object_tree(file)
        # Force an observable refresh before processing resumed objects.
        await asyncio.sleep(.01)
        for node in tree['blocks']:
            await dom._summarize_markdown_object_async(node['object'])
        dom._record_summary_progress('Section', '$.blocks[0]', file, {'count': 0}, {'count': 0})
        await asyncio.sleep(.01)
        tree['summary'] = 'document summary'
        return tree

    monkeypatch.setattr(dom, '_prepare_ignored_images', snapshot)
    monkeypatch.setattr(dom, 'extract_markdown_object_tree', lambda file: copy.deepcopy(original))
    monkeypatch.setattr(dom, '_object_nodes', lambda tree: iter(tree['blocks']))
    monkeypatch.setattr(dom, '_summarize_markdown_object_async', summarize)
    monkeypatch.setattr(dom, 'summarize_markdown_object_tree_async', tree_summary)
    monkeypatch.setattr(app, '_prepare_incremental_tree', lambda *args: ([], 0, False))
    options = dict(checkpoint_path=tmp_path / 'checkpoint.json', progress_factory=Progress,
                   progress_interval=.001, concurrency=1,
                   completed_callback=lambda rows: completion_updates.append(rows))
    async def scenario():
        nonlocal interrupt
        try:
            await app.summarize_object_documents([file], object(), 'offline', **options)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('expected cancellation')
        assert output.read_text() == 'previous output'
        assert app._partial_path(file).exists()
        calls.clear()
        observed.clear()
        interrupt = False
        rows = await app.summarize_object_documents([file], object(), 'offline', **options)
        assert calls == ['1']
        assert rows[0]['status'] == 'completed' and rows[0]['resumed']
        assert rows[0]['completed_objects'] == 2
        assert rows[0]['section_count'] == rows[0]['completed_sections'] == 1
        assert any('sections 0/1' in text for _, _, text in observed)
        assert any('sections 1/1' in text for _, _, text in observed)
        assert any(n == 1 and total == 2 for n, total, _ in observed)
        assert not app._partial_path(file).exists()
        assert completion_updates[0] == []
        assert completion_updates[-1][0]['file'] == str(file)
        assert completion_updates[-1][0]['status'] == 'completed'
    asyncio.run(scenario())


def test_section_count_includes_nested_sections_but_not_object_source():
    from utils.summary_object_asyncio import _section_count
    tree = {'blocks': [{'t': 'Section', 'c': [{'t': 'Header', 'c': []}],
                        'subnodes': [{'t': 'Section', 'c': []},
                                     {'t': 'Div', 'object': {},
                                      'c': [{'t': 'Section', 'c': []}]}]}]}
    assert _section_count(tree) == 2
