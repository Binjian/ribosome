"""Offline concurrency, backpressure and cancellation checks for pooled embeddings."""
import asyncio
import sys
from pathlib import Path
from collections import Counter
from types import SimpleNamespace

import pytest
from ollama import AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.embedding_ollama_pool import OllamaEmbeddingPool

HOSTS = ['http://localhost:11434', 'http://172.27.74.16:11434']
MODEL = 'qwen3-embedding:8b'


def host(client):
    return str(client._client.base_url).rstrip('/')


def test_two_hosts_are_used_concurrently_with_capacity_limits(monkeypatch):
    async def scenario():
        release, full = asyncio.Event(), asyncio.Event()
        active, peaks = Counter(), Counter()
        seen = []
        async def embed(self, **kwargs):
            url = host(self)
            active[url] += 1
            peaks[url] = max(peaks[url], active[url])
            seen.append((url, kwargs))
            if sum(active.values()) == 4:
                full.set()
            try:
                await release.wait()
                return {'embeddings': [[1.0, 2.0]], 'model': kwargs['model']}
            finally:
                active[url] -= 1
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=HOSTS, concurrency_per_server=2)
        tasks = [asyncio.create_task(pool.embed(model=MODEL, input=f'text {i}')) for i in range(8)]
        try:
            await asyncio.wait_for(full.wait(), 1)
            assert active == Counter(dict.fromkeys(HOSTS, 2))
            with pytest.raises(RuntimeError, match='active'):
                await pool.aclose()
            release.set()
            responses = await asyncio.gather(*tasks)
            assert all(response['model'] == MODEL for response in responses)
            assert peaks == Counter(dict.fromkeys(HOSTS, 2))
            assert all(kwargs['model'] == MODEL for _, kwargs in seen)
            assert sum(row['completed'] for row in pool.server_snapshots()) == 8
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.aclose()
        assert pool._slots.qsize() == 4
        assert all(server._client.is_closed for server in pool.servers)
    asyncio.run(scenario())


def test_faster_server_continues_while_other_server_is_busy(monkeypatch):
    async def scenario():
        release, started = asyncio.Event(), asyncio.Event()
        async def embed(self, **kwargs):
            if host(self) == HOSTS[0]:
                started.set()
                await release.wait()
            return host(self)
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=HOSTS)
        slow = asyncio.create_task(pool.embed(model=MODEL, input='slow'))
        try:
            await asyncio.wait_for(started.wait(), 1)
            for _ in range(3):
                assert await pool.embed(model=MODEL, input='fast') == HOSTS[1]
            assert not slow.done()
        finally:
            release.set()
            await slow
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['error', 'cancel', 'timeout'])
def test_requests_release_slots_on_failure_and_cancellation(monkeypatch, outcome):
    async def scenario():
        async def embed(self, **kwargs):
            if outcome == 'error':
                raise ValueError('offline failure')
            await asyncio.Future()
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=HOSTS, request_timeout=0.03)
        tasks = [asyncio.create_task(pool.embed(model=MODEL, input='x')) for _ in range(3)]
        try:
            if outcome == 'cancel':
                await asyncio.sleep(0)
                for task in tasks:
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            expected = {'error': RuntimeError, 'timeout': TimeoutError, 'cancel': asyncio.CancelledError}[outcome]
            assert all(isinstance(result, expected) for result in results)
            assert pool._inflight == 0 and pool._slots.qsize() == 2
            assert all(row['active'] == 0 for row in pool.server_snapshots())
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.aclose()
        with pytest.raises(RuntimeError, match='closed'):
            await pool.embed(model=MODEL, input='closed')
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [None, 'offline', 'not-embedding'])
def test_preflight_checks_both_hosts_concurrently_without_inference(monkeypatch, failure):
    async def scenario():
        seen, both = set(), asyncio.Event()
        async def show(self, model):
            assert model == MODEL
            url = host(self)
            seen.add(url)
            if len(seen) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            if url == HOSTS[1] and failure == 'offline':
                raise ConnectionError('unreachable')
            capabilities = ['completion'] if url == HOSTS[1] and failure == 'not-embedding' else ['embedding']
            return SimpleNamespace(capabilities=capabilities)
        monkeypatch.setattr(AsyncClient, 'show', show)
        pool = OllamaEmbeddingPool(hosts=HOSTS)
        try:
            if failure:
                with pytest.raises(RuntimeError, match=HOSTS[1]):
                    await pool.check_models(MODEL)
            else:
                assert await pool.check_models(MODEL) == MODEL
            assert seen == set(HOSTS)
            assert all(row['completed'] == 0 for row in pool.server_snapshots())
        finally:
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('options', [dict(hosts=[]), dict(hosts=['bad']), dict(hosts=HOSTS, concurrency_per_server=0),
    dict(hosts=HOSTS, concurrency_per_server=True), dict(hosts=HOSTS, request_timeout=float('nan'))])
def test_invalid_configuration(options):
    with pytest.raises(ValueError):
        OllamaEmbeddingPool(**options)
