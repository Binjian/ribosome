"""Offline routing, capacity, cancellation, and integration for two Ollama hosts."""
import asyncio
from collections import Counter
from types import SimpleNamespace
from pathlib import Path
import sys

from ollama import AsyncClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.summary_asyncio import _document_id
from utils.summary_ollama_pool import OllamaSummaryPool


HOSTS = ['http://127.0.0.1:11434', 'http://172.27.74.16:11434']


def host(client):
    return str(client._client.base_url).rstrip('/')


def test_routes_concurrently_with_per_server_limits_and_document_metrics(monkeypatch):
    async def scenario():
        release, filled = asyncio.Event(), asyncio.Event()
        active, peak, calls = Counter(), Counter(), []
        async def chat(self, **kwargs):
            server = host(self)
            active[server] += 1
            peak[server] = max(peak[server], active[server])
            calls.append((server, kwargs))
            if sum(active.values()) == 4: filled.set()
            try:
                await release.wait()
                return {'message': {'content': 'summary'}, 'eval_count': 3}
            finally:
                active[server] -= 1
        monkeypatch.setattr(AsyncClient, 'chat', chat)
        pool = OllamaSummaryPool(hosts=HOSTS, concurrency_per_server=2)
        token = _document_id.set('manual.md')
        tasks = []
        try:
            messages = [{'role': 'user', 'content': 'image', 'images': ['local.png']}]
            tasks = [asyncio.create_task(pool.chat(model='vision', messages=messages)) for _ in range(8)]
            await asyncio.wait_for(filled.wait(), 1)
            assert active == Counter(dict.fromkeys(HOSTS, 2))
            assert '2/2' in pool.server_status()
            with pytest.raises(RuntimeError, match='active'): pool.reset_metrics()
            release.set()
            await asyncio.gather(*tasks)
            assert peak == Counter(dict.fromkeys(HOSTS, 2))
            assert all(call['think'] is False and call['options']['num_predict'] == 2048
                       and call['messages'] == messages for _, call in calls)
            assert pool.metrics.snapshot(document='manual.md')['request_count'] == 8
            snapshots = pool.server_snapshots()
            assert {row['host'] for row in snapshots} == set(HOSTS)
            assert sum(row['request_count'] for row in snapshots) == 8
            assert all(row['peak_requests'] == 2 for row in snapshots)
            assert pool._slots.qsize() == 4 and not pool.active_requests
            pool.reset_metrics()
            assert all(row['request_count'] == 0 for row in pool.server_snapshots())
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            _document_id.reset(token)
            await pool.aclose()
        assert all(client._client.is_closed for client in pool.servers)
    asyncio.run(scenario())


def test_fast_server_keeps_working_while_other_is_busy(monkeypatch):
    async def scenario():
        release, slow_started = asyncio.Event(), asyncio.Event()
        async def chat(self, **kwargs):
            if host(self) == HOSTS[0]:
                slow_started.set()
                await release.wait()
            return host(self)
        monkeypatch.setattr(AsyncClient, 'chat', chat)
        pool = OllamaSummaryPool(hosts=HOSTS, concurrency_per_server=1)
        slow = asyncio.create_task(pool.chat(model='offline'))
        try:
            await asyncio.wait_for(slow_started.wait(), 1)
            for _ in range(4):
                assert await asyncio.wait_for(pool.chat(model='offline'), 1) == HOSTS[1]
            assert not slow.done()
        finally:
            release.set()
            await slow
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['cancel', 'timeout', 'error'])
def test_failure_and_queued_cancellation_return_slots(monkeypatch, outcome):
    async def scenario():
        filled = asyncio.Event()
        count = 0
        async def chat(self, **kwargs):
            nonlocal count
            count += 1
            if count == 2: filled.set()
            if outcome == 'error': raise ValueError('provider failed')
            await asyncio.Event().wait()
        monkeypatch.setattr(AsyncClient, 'chat', chat)
        pool = OllamaSummaryPool(hosts=HOSTS, concurrency_per_server=1,
                                 request_timeout=0.05 if outcome == 'timeout' else 3)
        tasks = [asyncio.create_task(pool.chat(model='offline')) for _ in range(3)]
        try:
            if outcome == 'cancel':
                await asyncio.wait_for(filled.wait(), 1)
                assert pool.metrics.active == 3
                with pytest.raises(RuntimeError, match='active'): await pool.aclose()
                for task in tasks: task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            kind = {'cancel': asyncio.CancelledError, 'timeout': TimeoutError, 'error': RuntimeError}[outcome]
            assert all(isinstance(result, kind) for result in results)
            if outcome == 'error': assert all('Ollama server http://' in str(r) for r in results)
            assert pool._slots.qsize() == 2
            assert pool.metrics.active == 0 and not pool.active_requests
            assert all(m.active == 0 for m in pool.server_metrics)
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [None, 'missing', 'offline'])
def test_preflight_checks_all_servers_concurrently(monkeypatch, failure):
    async def scenario():
        seen, both = set(), asyncio.Event()
        async def list_models(self):
            server = host(self)
            seen.add(server)
            if len(seen) == 2: both.set()
            await asyncio.wait_for(both.wait(), 1)
            if server == HOSTS[1] and failure == 'offline': raise ConnectionError('unreachable')
            name = 'different' if server == HOSTS[1] and failure == 'missing' else 'vision'
            return SimpleNamespace(models=[SimpleNamespace(model=name)])
        monkeypatch.setattr(AsyncClient, 'list', list_models)
        pool = OllamaSummaryPool(hosts=HOSTS)
        try:
            if failure:
                with pytest.raises(RuntimeError, match=HOSTS[1]): await pool.check_models('vision')
            else:
                assert await pool.check_models('vision') == 'vision'
            assert seen == set(HOSTS)
            assert pool.metrics.snapshot()['request_count'] == 0
        finally:
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('options', [dict(hosts=[]), dict(hosts=['invalid']),
    dict(hosts=[HOSTS[0], HOSTS[0] + '/']), dict(hosts=HOSTS, concurrency_per_server=0)])
def test_invalid_pool_configuration(options):
    with pytest.raises(ValueError): OllamaSummaryPool(**options)


def test_pool_integrates_with_object_batch_metrics_and_checkpoint(tmp_path, monkeypatch):
    from tqdm.auto import tqdm
    from ribosome.core.dom.summary import object as dom_summary
    from utils.summary_object_asyncio import summarize_object_documents

    files = [tmp_path / f'manual{i}.md' for i in range(2)]
    for file in files:
        file.write_text('| Header |\n| --- |\n| Value |\n')
    class SilentProgress(tqdm):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs, disable=True)
    async def chat(self, **kwargs):
        await asyncio.sleep(0)
        return {'message': {'content': 'summary'}, 'eval_count': 1}
    async def summary(client, content, **kwargs):
        response = await client.chat(model='vision', messages=[{'role': 'user', 'content': content}])
        return response['message']['content']
    monkeypatch.setattr(AsyncClient, 'chat', chat)
    monkeypatch.setattr(dom_summary, 'get_summary_response_async', summary)

    async def scenario():
        pool = OllamaSummaryPool(hosts=HOSTS, concurrency_per_server=1)
        try:
            async def batch(hosts):
                return await summarize_object_documents(
                    files, pool, 'vision', checkpoint_host=hosts, leaf_min_len=99999,
                    checkpoint_path=tmp_path / 'checkpoint.json', progress_factory=SilentProgress)
            rows = await batch(HOSTS)
            assert all(row['status'] == 'completed' and row['request_count'] == 3 for row in rows)
            assert all(row['request_count'] > 0 for row in pool.server_snapshots())
            pool.reset_metrics()
            assert all(row['resumed'] for row in await batch(HOSTS))
            assert pool.metrics.snapshot()['request_count'] == 0
            assert all(not row['resumed'] for row in await batch(HOSTS[:1]))
        finally:
            await pool.aclose()
    asyncio.run(scenario())
