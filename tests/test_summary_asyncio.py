"""Offline acceptance tests for the operational summary notebook helpers."""

import ast
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

from ollama import AsyncClient
import pytest

from ribosome.core.dom import model as dom_model, summary as dom_summary
from utils import summary_asyncio as app


class Progress:
    instances = []
    messages = []

    def __init__(self, total, **kwargs):
        self.total = total
        self.n = 0
        self.refreshes = 0
        self.postfixes = 0
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def refresh(self):
        self.refreshes += 1

    def set_postfix(self, **kwargs):
        assert kwargs['refresh'] is False
        self.postfixes += 1

    def set_postfix_str(self, text, *, refresh):
        assert refresh is False
        self.postfixes += 1

    @classmethod
    def write(cls, message):
        cls.messages.append(message)


@pytest.fixture(autouse=True)
def reset_progress():
    Progress.instances = []
    Progress.messages = []


def fake_factory(payload, *, setup_hook=None, textualize_hook=None):
    class FakeDOM:
        def __init__(self, file, **kwargs):
            self.file = file
            self.ast_json = None
            self.semantics_json = None
            self.client = kwargs['ollama_client']
            self.model = kwargs['ollama_model']

        def setup(self):
            if setup_hook:
                setup_hook(self)
            self.ast_json = json.dumps(deepcopy(payload))

        async def textualize(self):
            if textualize_hook:
                await textualize_hook(self)
            blocks = await dom_model.summarize_node_async(
                json.loads(self.ast_json)['blocks'], Path('.'), self.client, self.model,
                self.file, {'count': 0}, {'count': 0}, leaf_min_len=99999, min_len=99999)
            self.semantics_json = json.dumps({'blocks': blocks})

    return FakeDOM


def test_batch_counts_nodes_without_rendering_per_node():
    payload = {'blocks': [{'t': 'Str', 'c': f'node-{i}'} for i in range(500)]}
    original = dom_summary.summarize_node_async
    rows = asyncio.run(app.summarize_documents(
        ['one.md', 'two.md'], object(), 'offline', object(),
        progress_interval=60, dom_factory=fake_factory(payload), progress_factory=Progress))
    assert [row['status'] for row in rows] == ['completed', 'completed']
    documents, nodes = Progress.instances
    assert (documents.n, documents.total) == (2, 2)
    assert (nodes.n, nodes.total) == (1002, 1002)
    assert nodes.refreshes == nodes.postfixes == 2  # Initial render and final flush only.
    assert all(bar.closed for bar in Progress.instances)
    assert dom_summary.summarize_node_async is original
    assert dom_model.summarize_node_async is original


def test_setup_runs_in_threads_without_blocking_other_documents():
    async def scenario():
        loop = asyncio.get_running_loop()
        setup_started = asyncio.Event()
        quick_completed = asyncio.Event()
        release = threading.Event()
        thread_ids = []
        construction_ids = []

        def setup(dom):
            thread_ids.append(threading.get_ident())
            if dom.file.name == 'blocked.md':
                loop.call_soon_threadsafe(setup_started.set)
                assert release.wait(3)

        async def textualize(dom):
            if dom.file.name == 'quick.md':
                quick_completed.set()

        factory = fake_factory({'blocks': [{'t': 'Str', 'c': 'text'}]},
                               setup_hook=setup, textualize_hook=textualize)

        def construct(*args, **kwargs):
            construction_ids.append(threading.get_ident())
            return factory(*args, **kwargs)

        task = asyncio.create_task(app.summarize_documents(
            ['blocked.md', 'quick.md'], object(), 'offline', object(), concurrency=2,
            dom_factory=construct, progress_factory=Progress))
        try:
            await asyncio.wait_for(setup_started.wait(), 2)
            await asyncio.wait_for(quick_completed.wait(), 2)
            assert not task.done()
            assert all(i != threading.get_ident() for i in thread_ids)
            assert construction_ids == [threading.get_ident()] * 2
            release.set()
            assert all(row['status'] == 'completed' for row in await task)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_cancellation_awaits_setup_and_restores_hooks_even_if_cancelled_twice():
    async def scenario():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        summarized = []
        original = dom_summary.summarize_node_async

        def setup(dom):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3)
            finished.set()

        async def textualize(dom):
            summarized.append(dom.file)

        task = asyncio.create_task(app.summarize_documents(
            ['blocked.md'], object(), 'offline', object(),
            dom_factory=fake_factory({'blocks': []}, setup_hook=setup, textualize_hook=textualize),
            progress_factory=Progress))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert finished.is_set()
            assert not summarized
            assert dom_summary.summarize_node_async is original
            assert dom_model.summarize_node_async is original
            assert all(bar.closed for bar in Progress.instances)
            assert not app._batch_active
            assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_failures_do_not_stop_later_documents_or_leave_unfinished_node_totals():
    async def fail(dom):
        if dom.file.name == 'bad.md':
            raise RuntimeError('provider unavailable')

    rows = asyncio.run(app.summarize_documents(
        ['bad.md', 'good.md'], object(), 'offline', object(), concurrency=1,
        dom_factory=fake_factory({'blocks': [{'t': 'Str', 'c': 'text'}]}, textualize_hook=fail),
        progress_factory=Progress))
    assert [row['status'] for row in rows] == ['failed', 'completed']
    assert rows[0]['error'] == 'RuntimeError: provider unavailable'
    assert (Progress.instances[1].n, Progress.instances[1].total) == (2, 2)


def test_request_metrics_include_provider_timings_and_failure_cleanup(monkeypatch):
    async def response(self, **kwargs):
        await asyncio.sleep(0)
        if kwargs['model'] == 'fail':
            raise RuntimeError('provider failed')
        return SimpleNamespace(message=SimpleNamespace(content='text'),
                               load_duration=1_000_000_000, prompt_eval_duration=2_000_000_000,
                               eval_duration=3_000_000_000, prompt_eval_count=4, eval_count=5)

    monkeypatch.setattr(AsyncClient, 'chat', response)

    async def scenario():
        client = app.SummaryAsyncClient()
        try:
            token = app._document_id.set('doc')
            try:
                await asyncio.gather(client.chat(model='good'), client.chat(model='good'))
                with pytest.raises(RuntimeError, match='provider failed'):
                    await client.chat(model='fail')
            finally:
                app._document_id.reset(token)
            stats = client.metrics.snapshot(document='doc', wall_seconds=2)
            assert stats['request_count'] == 3
            assert stats['peak_requests'] == 2
            assert stats['request_errors'] == 1
            assert stats['load_seconds'] == 2
            assert stats['prompt_eval_seconds'] == 4
            assert stats['generation_seconds'] == 6
            assert stats['prompt_tokens'] == 8
            assert stats['generated_tokens'] == 10
            assert stats['requests_per_second'] == 1.5
            assert stats['p95_latency_seconds'] >= stats['mean_latency_seconds'] > 0
            assert client.metrics.active == 0
            client.reset_metrics()
            assert client.metrics.snapshot()['request_count'] == 0
        finally:
            await client._client.aclose()

    asyncio.run(scenario())


def test_notebook_configuration_preserves_overrides_and_detects_stale_imports(monkeypatch):
    names = ['RIBOSOME_DOCUMENT_CONCURRENCY', 'RIBOSOME_SUMMARY_CONCURRENCY', 'RIBOSOME_SUMMARY_TASK_LIMIT']
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delitem(sys.modules, 'ribosome.core.dom.summary')
    assert list(app.configure_summary_limits().values()) == [4, 8, 32]
    monkeypatch.setenv(names[1], '2')
    assert app.configure_summary_limits()[names[1]] == 2
    monkeypatch.setitem(sys.modules, 'ribosome.core.dom.summary',
                        SimpleNamespace(_SUMMARY_CONCURRENCY=8, _SUMMARY_TASK_LIMIT=32))
    with pytest.raises(RuntimeError, match='Restart the kernel'):
        app.configure_summary_limits()
    monkeypatch.setenv(names[0], '0')
    with pytest.raises(ValueError, match='positive integer'):
        app.configure_summary_limits()


def test_notebook_compiles_and_configures_before_dom_import():
    from nbformat import read, validate
    from IPython.core.interactiveshell import InteractiveShell
    notebook = read(Path('nbs/05.application.summary.ipynb'), as_version=4)
    validate(notebook)
    shell = InteractiveShell.instance()
    for cell in notebook.cells:
        if cell.cell_type == 'code':
            compile(shell.transform_cell(cell.source), '<notebook>', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            assert cell.outputs == []
            assert cell.execution_count is None
    config = notebook.cells[2].source
    assert config.index('configure_summary_limits()') < config.index('from ribosome.core.dom.model import DOMClass')
    assert 'SummaryAsyncClient(host=OLLAMA_HOST)' in config
    assert 'await summarize_documents(' in notebook.cells[6].source


def test_batch_replaces_semantics_and_preserves_cached_ast(tmp_path, monkeypatch):
    import chromadb
    monkeypatch.setattr(dom_model.DOMClass, 'leaf_min_len', 99999)
    monkeypatch.setattr(dom_model.DOMClass, 'min_len', 99999)
    files = [tmp_path / 'one.md', tmp_path / 'two.md']
    original = json.dumps({'blocks': [{'t': 'Str', 'c': 'fresh'}, {'t': 'Space'}]}).encode()
    for file in files:
        file.write_text('cached document', encoding='utf-8')
        file.with_name(f'{file.stem}_ast.json').write_bytes(original)
        file.with_name(f'{file.stem}_semantics.json').write_text('{"summary":"stale"}', encoding='utf-8')
    async def run():
        client = app.SummaryAsyncClient()
        try:
            return await app.summarize_documents(
                files, client, 'offline', chromadb.EphemeralClient(),
                concurrency=2, progress_factory=Progress)
        finally:
            await client._client.aclose()

    rows = asyncio.run(run())
    assert all(row['status'] == 'completed' for row in rows)
    for file in files:
        assert file.with_name(f'{file.stem}_ast.json').read_bytes() == original
        semantics = json.loads(file.with_name(f'{file.stem}_semantics.json').read_text())
        assert semantics['blocks'][0]['s'] == 'fresh'
        assert semantics['blocks'][1]['s'] == ''
        assert semantics['summary'] != 'stale'


def test_metrics_release_inflight_counts_on_cancellation(monkeypatch):
    async def scenario():
        started = asyncio.Event()

        async def response(self, **kwargs):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(AsyncClient, 'chat', response)
        client = app.SummaryAsyncClient()
        try:
            task = asyncio.create_task(client.chat(model='offline'))
            await started.wait()
            with pytest.raises(RuntimeError, match='requests are active'):
                client.reset_metrics()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert client.metrics.active == 0
            assert client.metrics.snapshot()['cancelled_requests'] == 1
        finally:
            await client._client.aclose()

    asyncio.run(scenario())


def test_benchmark_samples_and_outputs_are_isolated(tmp_path):
    from utils.benchmark_summary_asyncio import cached_samples
    source = tmp_path / 'document_ast.json'
    image = tmp_path / 'figure.png'
    image.write_bytes(b'fixture')
    payload = {'blocks': [
        {'t': 'Para', 'c': [{'t': 'Str', 'c': 'text ' * 150}]},
        {'t': 'Image', 'c': [[], [], ['figure.png', '']]},
    ]}
    source.write_text(json.dumps(payload))
    before = source.read_bytes()
    samples = cached_samples(tmp_path)
    assert {'text-0', 'images', 'nested'} == {sample['name'] for sample in samples}
    assert samples[1]['blocks'][0]['c'][2][0] == str(image)
    samples[0]['blocks'][0]['s'] = 'temporary'
    assert source.read_bytes() == before
    source.write_text(json.dumps({'blocks': payload['blocks'][1:]}))
    with pytest.raises(ValueError, match='cached text ASTs'):
        cached_samples(tmp_path)


def test_offline_benchmark_preserves_calls_and_temporary_outputs(tmp_path):
    from utils.benchmark_summary_asyncio import benchmark
    args = SimpleNamespace(live=False, host=None, model=None, input_dir=tmp_path,
                           output_dir=tmp_path / 'benchmark', mock_latency=0.001,
                           baseline_task_limit=8, task_limit=32, request_limit=8,
                           document_limit=4, repeats=1, server_parallel=None, timeout_seconds=5)
    report = asyncio.run(benchmark(args))
    assert len(report['warmups']) == len(report['runs']) == 2
    assert [row['request_counts'] for row in report['summary']] == [[48], [48]]
    assert all(row['failed_samples'] == row['request_errors'] == 0 for row in report['summary'])
    for index in range(3):
        baseline = json.loads((args.output_dir / f'baseline-0-{index}.json').read_text())
        optimized = json.loads((args.output_dir / f'optimized-0-{index}.json').read_text())
        assert optimized == baseline
    assert json.loads((args.output_dir / 'report.json').read_text())['mode'] == 'mock'
    assert (args.output_dir / 'samples.json').is_file()


def test_benchmark_timeout_cleans_up_requests_and_saves_diagnostics(tmp_path):
    from utils.benchmark_summary_asyncio import benchmark
    args = SimpleNamespace(live=False, host=None, model=None, input_dir=tmp_path,
                           output_dir=tmp_path / 'timeout', mock_latency=1,
                           baseline_task_limit=8, task_limit=32, request_limit=8,
                           document_limit=4, repeats=3, server_parallel=None, timeout_seconds=0.01)

    async def scenario():
        with pytest.raises(RuntimeError, match='Benchmark had failures'):
            await benchmark(args)
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(scenario())
    report = json.loads((args.output_dir / 'report.json').read_text())
    assert report['summary'] == report['runs'] == []
    assert len(report['warmups']) == 1
    assert 'Timed out' in report['warmups'][0]['error']
    assert report['warmups'][0]['cancelled_requests'] > 0


def test_checkpoint_resumes_after_cancellation_and_invalidates_changes(tmp_path):
    files = [tmp_path / 'one.md', tmp_path / 'two.md']
    for file in files:
        file.write_text('text')
    checkpoint = tmp_path / 'checkpoint.json'
    calls = []
    interrupt = True

    async def textualize(dom):
        calls.append(dom.file.name)
        if interrupt and dom.file.name == 'two.md':
            raise asyncio.CancelledError
        dom.file.with_name(f'{dom.file.stem}_semantics.json').write_text('{"blocks": []}')

    def run(model='offline', resume=True):
        return asyncio.run(app.summarize_documents(
            files, object(), model, object(), concurrency=1,
            checkpoint_path=checkpoint, resume=resume, checkpoint_host='local',
            dom_factory=fake_factory({'blocks': []}, textualize_hook=textualize),
            progress_factory=Progress))

    with pytest.raises(asyncio.CancelledError):
        run()
    assert json.loads(checkpoint.read_text())['entries'][str(files[0].resolve())]['row']['status'] == 'completed'
    interrupt = False
    calls.clear()
    rows = run()
    assert calls == ['two.md']
    assert [row['resumed'] for row in rows] == [True, False]
    calls.clear()
    assert all(row['resumed'] for row in run())
    assert calls == []
    files[0].write_text('changed')
    run()
    assert calls == ['one.md']
    calls.clear()
    run(model='new-model')
    assert calls == ['one.md', 'two.md']
    calls.clear()
    run(model='new-model', resume=False)
    assert calls == ['one.md', 'two.md']


def test_checkpoint_retries_failures_and_missing_outputs(tmp_path):
    file = tmp_path / 'doc.md'
    file.write_text('text')
    checkpoint = app.SummaryCheckpoint(tmp_path / 'state.json', 'model', 'host', True)
    row = {'file': str(file), 'status': 'failed'}
    checkpoint.record(file, row)
    assert checkpoint.completed(file) is None
    row['status'] = 'completed'
    checkpoint.record(file, row)
    assert checkpoint.completed(file) is None
    output = file.with_name('doc_semantics.json')
    output.write_text('{}')
    checkpoint.record(file, row)
    assert checkpoint.completed(file)['resumed']
    output.write_text('{"changed": true}')
    assert checkpoint.completed(file) is None
