"""Bounded notebook inference and progress after outer objects finish."""
import asyncio
from types import SimpleNamespace

import pytest

from ribosome.core.dom.summary import object as sut


@pytest.mark.parametrize('outcome', ['success', 'timeout', 'cancel'])
def test_notebook_client_bounds_generation_and_releases_request_state(monkeypatch, outcome):
    calls, active = [], set()
    async def chat(self, *args, **kwargs):
        calls.append(kwargs)
        identity = object()
        active.add(identity)
        try:
            if outcome != 'success': await asyncio.Event().wait()
            return SimpleNamespace(message=SimpleNamespace(content='summary'))
        finally:
            active.remove(identity)
    monkeypatch.setattr(sut.AsyncClient, 'chat', chat)
    async def run():
        client = sut._NotebookSummaryClient(host='http://offline', headers={}, request_timeout=0.02, max_tokens=123)
        try:
            token = sut._summary_stage.set('final document summary')
            try:
                request = client.chat(model='offline', messages=[{'role': 'user', 'content': 'text'}])
                if outcome == 'success':
                    assert (await request).message.content == 'summary'
                elif outcome == 'timeout':
                    with pytest.raises(TimeoutError, match='during final document summary'):
                        await request
                else:
                    task = asyncio.create_task(request)
                    await asyncio.sleep(0)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError): await task
            finally:
                sut._summary_stage.reset(token)
            assert not client._summary_requests['active'] and not active
            assert calls[0]['think'] is False and calls[0]['options']['num_predict'] == 123
            assert client._summary_requests['started'] == 1
            assert client._summary_requests['completed'] == (1 if outcome == 'success' else 0)
        finally:
            await client._client.aclose()
    asyncio.run(run())


def test_notebook_client_preserves_explicit_generation_options(monkeypatch):
    async def chat(self, *args, **kwargs): return kwargs
    monkeypatch.setattr(sut.AsyncClient, 'chat', chat)
    async def run():
        client = sut._NotebookSummaryClient(host='http://offline', headers={})
        try:
            result = await client.chat(model='offline', think=True, options={'num_predict': 7, 'temperature': 0})
            assert result['think'] is True
            assert result['options'] == {'num_predict': 7, 'temperature': 0}
        finally:
            await client._client.aclose()
    asyncio.run(run())


def test_timeout_during_final_document_summary_closes_progress_and_keeps_previous_output(tmp_path, monkeypatch):
    file = tmp_path / 'manual.md'
    file.write_text('- item\n')
    output = tmp_path / 'manual_semantics.json'
    output.write_text('previous')
    phases, bars = [], []
    class Bar:
        def __init__(self, **kwargs):
            self.total, self.completed, self.closed, self.statuses = kwargs['total'], 0, False, []
            bars.append(self)
        def update(self, count): self.completed += count
        def close(self): self.closed = True
        def set_postfix_str(self, status, **kwargs): self.statuses.append(status)
    async def chat(self, *args, **kwargs):
        phase = sut._summary_stage.get()
        phases.append(phase)
        if phase == 'final document summary': await asyncio.Event().wait()
        return SimpleNamespace(message=SimpleNamespace(content='object summary'))
    async def summary(client, content, **kwargs):
        return (await client.chat(model='offline', messages=[{'role': 'user', 'content': content}])).message.content
    monkeypatch.setattr(sut.AsyncClient, 'chat', chat)
    monkeypatch.setattr(sut, 'get_summary_response_async', summary)
    monkeypatch.setattr('tqdm.auto.tqdm', Bar)
    async def run():
        client = sut._NotebookSummaryClient(host='http://offline', headers={}, request_timeout=0.02)
        try:
            with pytest.raises(TimeoutError, match='final document summary'):
                tree = await sut.summarize_markdown_object_tree_async(file, client, 'offline', show_progress=True, leaf_min_len=1)
                sut._write_semantics_tree(output, tree)
            assert not client._summary_requests['active']
            assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
            assert sut._summary_stage.get() == 'object summary'
            assert sut._object_summary_progress.get() is None
        finally:
            await client._client.aclose()
    asyncio.run(run())
    assert phases[-1] == 'final document summary'
    assert bars[0].completed == bars[0].total == 1 and bars[0].closed and bars[0].statuses
    assert output.read_text() == 'previous'


def test_request_monitor_reports_active_final_summary_and_age():
    statuses = []
    class Progress:
        def set_postfix_str(self, status, **kwargs): statuses.append(status)
    async def run():
        client = SimpleNamespace(_summary_requests={'started': 3, 'completed': 2,
            'active': {3: (sut.perf_counter() - 12, 'final document summary', 4000)}})
        task = asyncio.create_task(sut._watch_summary_requests(client, Progress()))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    assert 'requests 2/3, active=1' in statuses[0]
    assert 'final document summary' in statuses[0] and '4000 chars' in statuses[0]


@pytest.mark.parametrize('timeout,tokens', [('0', '12'), ('nan', '12'), ('inf', '12'), ('bad', '12'), ('1', '0')])
def test_invalid_notebook_request_limits_are_rejected(tmp_path, monkeypatch, timeout, tokens):
    (tmp_path / 'pyproject.toml').write_text('')
    (tmp_path / '.env').write_text('OLLAMA_HOST=http://offline\nOLLAMA_SUMMARY_MODEL=offline\n')
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path))
    monkeypatch.setenv('RIBOSOME_SUMMARY_REQUEST_TIMEOUT', timeout)
    monkeypatch.setenv('RIBOSOME_SUMMARY_MAX_TOKENS', tokens)
    with pytest.raises(ValueError, match='RIBOSOME_SUMMARY_'):
        sut._configured_ollama_summary_client()
