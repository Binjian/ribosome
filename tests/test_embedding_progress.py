"""Live concurrent embedding status and notebook cancellation finalization."""
import ast
import asyncio
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from nbformat import read
from ollama import AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ribosome.core.dom.embedding.object import IndexingResult
from utils.embedding_ollama_pool import OllamaEmbeddingPool
from utils.embedding_progress import EmbeddingProgressDisplay


HOSTS = ['http://localhost:11434', 'http://172.27.74.16:11434']


def test_status_snapshot_reports_eight_running_one_queued_and_cleans_up(monkeypatch):
    async def scenario():
        entered = 0
        filled = asyncio.Event()
        tick = [10.0]
        monkeypatch.setattr('utils.embedding_ollama_pool.perf_counter', lambda: tick[0])
        async def embed(self, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 8:
                filled.set()
            await asyncio.Future()
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=HOSTS, concurrency_per_server=4)
        tasks = [asyncio.create_task(pool.embed(model='qwen3-embedding:8b', input='x')) for _ in range(9)]
        try:
            await asyncio.wait_for(filled.wait(), 1)
            tick[0] = 15.0
            snapshot = pool.status_snapshot()
            assert snapshot['running'] == snapshot['capacity'] == 8
            assert snapshot['queued'] == 1
            assert snapshot['oldest_seconds'] == 5.0
            assert [server['active'] for server in snapshot['servers']] == [4, 4]
            assert all(server['capacity'] == 4 and server['oldest_seconds'] == 5.0 for server in snapshot['servers'])
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            final = pool.status_snapshot()
            assert final['running'] == final['queued'] == 0
            assert final['oldest_seconds'] == 0
            assert final['cancelled'] == 9
            await pool.aclose()
    asyncio.run(scenario())


def test_display_updates_one_panel_escapes_sources_and_distinguishes_document_errors():
    snapshot = dict(running=5, queued=2, capacity=8, oldest_seconds=3.0,
                    completed=12, failed=1, cancelled=0, servers=[
                        dict(host=HOSTS[0], active=4, capacity=4, completed=8, failed=0, cancelled=0, oldest_seconds=3.0),
                        dict(host=HOSTS[1], active=1, capacity=4, completed=4, failed=1, cancelled=0, oldest_seconds=2.0)])
    tick = [10.0]
    panels, updates = [], []
    handle = SimpleNamespace(update=lambda content: updates.append(content.data))
    def display(content, **kwargs):
        panels.append((content.data, kwargs))
        return handle
    monitor = EmbeddingProgressDisplay(SimpleNamespace(status_snapshot=lambda: snapshot), total=3,
        display_fn=display, clock=lambda: tick[0])
    report = [IndexingResult('done<1>.json', 'indexed'), IndexingResult('bad.json', 'failed')]
    monitor.update(current_file='file<script>.json', report=report)
    tick[0] = 15.0
    monitor.update(current_file='file<script>.json', report=report)
    assert len(panels) == 1 and panels[0][1] == {'display_id': True}
    html = updates[-1]
    assert 'Requests running 5/8' in html and 'Queued 2' in html
    assert 'Document failures 1' in html and 'Documents 2/3' in html
    assert HOSTS[0] in html and HOSTS[1] in html
    assert 'file&lt;script&gt;.json' in html and 'file<script>.json' not in html
    assert 'done&lt;1&gt;.json' in html and 'bad.json' not in html
    assert 'Current file (5.0s)' in html
    snapshot.update(running=0, queued=0, cancelled=1)
    monitor.update(current_file='file<script>.json', report=report, state='Cancelled')
    assert 'Embedding status: Cancelled' in updates[-1]
    assert 'Requests running 0/8' in updates[-1]


@pytest.mark.parametrize('cancel', [False, True])
def test_notebook_refreshes_during_requests_and_flushes_after_cleanup(tmp_path, monkeypatch, cancel):
    import utils.embedding_progress as progress_module
    notebook = read('nbs/01.core.dom.embedding.object.ipynb', as_version=4)
    source = next(cell.source for cell in notebook.cells
                  if cell.cell_type == 'code' and 'concurrency_display = ' in cell.source)
    code = compile(source, '<live-embedding-cell>', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    file = tmp_path / 'sample_semantics.json'
    file.write_text('{}')
    snapshots = []
    started = asyncio.Event()
    cleaned = []
    class Pool:
        def __init__(self, **kwargs): self.active = 0
        async def check_models(self, model): return model
        async def aclose(self): pass
        def server_snapshots(self): return []
    class Index:
        def __init__(self, *args, **kwargs): self.pool = kwargs['llm_client']
        async def index_paths(self, files):
            self.pool.active = 8
            started.set()
            try:
                if cancel:
                    await asyncio.Future()
                else:
                    await asyncio.sleep(0.3)
                return [IndexingResult(str(files[0]), 'indexed')]
            finally:
                self.pool.active = 0
                cleaned.append(True)
    class Display:
        def __init__(self, pool, **kwargs): self.pool = pool
        def update(self, *, current_file, report, state='Running'):
            snapshots.append((self.pool.active, len(report), state))
    class Bar:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def set_postfix(self, **kwargs): pass
        def refresh(self): pass
        def update(self, count): pass
        def write(self, text): pass
    monkeypatch.setattr(progress_module, 'EmbeddingProgressDisplay', Display)
    monkeypatch.setattr('tqdm.auto.tqdm', Bar)
    namespace = dict(Path=Path, INPUT_DIRS=[tmp_path], INDEX_DIR=tmp_path / 'db',
                     OLLAMA_HOSTS=HOSTS, SERVER_CONCURRENCY=4, REQUEST_TIMEOUT=300,
                     EMBED_MODEL='qwen3-embedding:8b', EMBEDDING_CONFIG={},
                     EMBEDDING_CONCURRENCY=8, headers={}, OllamaEmbeddingPool=Pool,
                     ObjectEmbeddingIndex=Index, asdict=asdict)
    async def scenario():
        task = asyncio.create_task(eval(code, namespace))
        await started.wait()
        await asyncio.sleep(0.26)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    asyncio.run(scenario())
    assert any(active == 8 for active, _, _ in snapshots)
    assert cleaned
    assert snapshots[-1] == (0, 0 if cancel else 1, 'Cancelled' if cancel else 'Completed')
