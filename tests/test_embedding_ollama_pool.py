"""Offline concurrency, backpressure and cancellation checks for pooled embeddings."""
import asyncio
import sys
from pathlib import Path
from collections import Counter
from types import SimpleNamespace

import pytest
import httpx
from ollama import AsyncClient, ResponseError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.embedding_ollama_pool import HEALTH_CHECK_TEXT, OllamaEmbeddingPool

HOSTS = ['http://localhost:11434', 'http://172.27.74.16:11434']
MODEL = 'qwen3-embedding:8b'


def host(client):
    return str(client._client.base_url).rstrip('/')


def test_preflight_and_document_requests_bypass_environment_proxies(monkeypatch):
    requests = []
    for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
        monkeypatch.setenv(key, 'http://proxy.invalid:8080')
    for key in ['NO_PROXY', 'no_proxy']:
        monkeypatch.setenv(key, '')
    def transport(*, proxy=None, **kwargs):
        async def handle(request):
            if proxy is not None:
                raise httpx.ConnectError('Environment proxy must not receive private-server requests')
            assert request.headers['authorization'] == 'Bearer offline-secret'
            requests.append(request)
            if request.url.path == '/api/show':
                return httpx.Response(200, json={'capabilities': ['embedding'], 'model_info': {}})
            assert request.url.path == '/api/embed'
            return httpx.Response(200, json={'embeddings': [[1.0, 2.0]], 'model': MODEL})
        return httpx.MockTransport(handle)
    monkeypatch.setattr('httpx._client.AsyncHTTPTransport', transport)
    async def scenario():
        server = 'http://172.27.74.49:11434'
        pool = OllamaEmbeddingPool(hosts=[server], headers={'Authorization': 'Bearer offline-secret'})
        try:
            assert await pool.check_models(MODEL) == MODEL
            assert (await pool.embed(model=MODEL, input='document'))['embeddings'] == [[1.0, 2.0]]
            assert [request.url.path for request in requests] == ['/api/show', '/api/embed', '/api/embed']
            assert all(str(request.url).startswith(server + '/') for request in requests)
            assert pool.hosts == [server]
            assert pool.status_snapshot()['retries'] == 0
        finally:
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('stalled_phase', ['model metadata', 'synthetic embedding'])
def test_health_timeout_identifies_the_stalled_phase(monkeypatch, stalled_phase):
    async def show(self, model):
        if stalled_phase == 'model metadata':
            await asyncio.Future()
        return {'capabilities': ['embedding']}
    async def embed(self, **kwargs):
        await asyncio.Future()
    monkeypatch.setattr(AsyncClient, 'show', show)
    monkeypatch.setattr(AsyncClient, 'embed', embed)
    async def scenario():
        pool = OllamaEmbeddingPool(hosts=[HOSTS[0]], health_timeout=0.01)
        try:
            with pytest.raises(RuntimeError, match=f'during {stalled_phase}'):
                await pool.check_models(MODEL)
            assert pool.server_snapshots()[0]['health_failures'] == 1
        finally:
            await pool.aclose()
    asyncio.run(scenario())


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
        assert pool._available == [2, 2]
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
            if kwargs['input'] == HEALTH_CHECK_TEXT:
                return {'embeddings': [[1.0, 2.0]]}
            if outcome == 'error':
                raise ValueError('offline failure')
            await asyncio.Future()
        async def show(self, model):
            return {'capabilities': ['embedding']}
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        monkeypatch.setattr(AsyncClient, 'show', show)
        pool = OllamaEmbeddingPool(hosts=HOSTS, request_timeout=0.03, request_retries=0, quarantine_seconds=0)
        tasks = [asyncio.create_task(pool.embed(model=MODEL, input='x')) for _ in range(3)]
        try:
            if outcome == 'cancel':
                await asyncio.sleep(0)
                for task in tasks:
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            expected = {'error': RuntimeError, 'timeout': TimeoutError, 'cancel': asyncio.CancelledError}[outcome]
            assert all(isinstance(result, expected) for result in results)
            assert pool._inflight == 0 and pool._available == [1, 1]
            assert all(row['active'] == 0 for row in pool.server_snapshots())
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.aclose()
        with pytest.raises(RuntimeError, match='closed'):
            await pool.embed(model=MODEL, input='closed')
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [None, 'offline', 'not-embedding', 'inference-stalled'])
def test_preflight_probes_both_hosts_concurrently_and_quarantines_bad_host(monkeypatch, failure):
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
        probes = []
        async def embed(self, **kwargs):
            assert kwargs == {'model': MODEL, 'input': HEALTH_CHECK_TEXT}
            probes.append(host(self))
            if host(self) == HOSTS[1] and failure == 'inference-stalled':
                await asyncio.Future()
            return SimpleNamespace(embeddings=[[1.0, 2.0]])
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=HOSTS, health_timeout=0.03)
        try:
            assert await pool.check_models(MODEL) == MODEL
            assert seen == set(HOSTS)
            assert HOSTS[0] in probes
            assert (HOSTS[1] in probes) == (failure in {None, 'inference-stalled'})
            assert all(row['completed'] == 0 for row in pool.server_snapshots())
            assert [row['health'] for row in pool.server_snapshots()] == ['healthy', 'quarantined' if failure else 'healthy']
            assert pool.hosts == HOSTS
        finally:
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('options', [dict(hosts=[]), dict(hosts=['bad']), dict(hosts=HOSTS, concurrency_per_server=0),
    dict(hosts=HOSTS, concurrency_per_server=True), dict(hosts=HOSTS, request_timeout=float('nan'))])
def test_invalid_configuration(options):
    with pytest.raises(ValueError):
        OllamaEmbeddingPool(**options)


@pytest.mark.parametrize('error', [TimeoutError('slow'), httpx.ReadTimeout('slow'),
    httpx.ConnectError('offline'), ResponseError('busy', status_code=503)])
def test_transient_failure_fails_over_without_changing_configured_hosts(monkeypatch, error):
    async def scenario():
        calls = []
        async def embed(self, **kwargs):
            calls.append(host(self))
            if host(self) == HOSTS[0]:
                raise error
            return {'embeddings': [[2.0, 1.0]]}
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=HOSTS, retry_backoff=0)
        try:
            assert await pool.embed(model=MODEL, input='document') == {'embeddings': [[2.0, 1.0]]}
            await pool.embed(model=MODEL, input='next')
            assert calls == [HOSTS[0], HOSTS[1], HOSTS[1]]
            assert pool.hosts == HOSTS
            snapshot = pool.status_snapshot()
            assert snapshot['retries'] == snapshot['failed'] == 1
            assert snapshot['completed'] == 2
            assert snapshot['healthy_capacity'] == 1
            assert snapshot['servers'][0]['health'] == 'quarantined'
        finally:
            await pool.aclose()
    asyncio.run(scenario())


def test_single_server_waits_for_quarantine_then_checks_health_and_retries(monkeypatch):
    async def scenario():
        calls = []
        async def show(self, model):
            calls.append(('show', asyncio.get_running_loop().time()))
            return {'capabilities': ['embedding']}
        async def embed(self, **kwargs):
            calls.append((kwargs['input'], asyncio.get_running_loop().time()))
            if len(calls) == 1:
                raise TimeoutError('temporary')
            return {'embeddings': [[2.0, 1.0]]}
        monkeypatch.setattr(AsyncClient, 'show', show)
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=[HOSTS[0]], quarantine_seconds=0.02, retry_backoff=0)
        try:
            await pool.embed(model=MODEL, input='document')
            assert [name for name, _ in calls] == ['document', 'show', HEALTH_CHECK_TEXT, 'document']
            assert calls[1][1] - calls[0][1] >= 0.018
            snapshot = pool.status_snapshot()
            assert snapshot['retries'] == snapshot['failed'] == snapshot['completed'] == 1
            assert snapshot['servers'][0]['health_checks'] == 1
            assert snapshot['servers'][0]['health'] == 'healthy'
            assert pool._available == [1]
        finally:
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('error', [ValueError('invalid input'), ResponseError('bad request', status_code=400),
                                  ResponseError('model missing', status_code=404)])
def test_permanent_errors_are_not_retried(monkeypatch, error):
    async def scenario():
        calls = []
        async def embed(self, **kwargs):
            calls.append(kwargs)
            raise error
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=[HOSTS[0]], retry_backoff=0)
        try:
            with pytest.raises(RuntimeError, match='1 attempts'):
                await pool.embed(model=MODEL, input='document')
            assert len(calls) == 1
            assert pool.status_snapshot()['retries'] == 0
            assert pool.server_snapshots()[0]['health'] == 'healthy'
        finally:
            await pool.aclose()
    asyncio.run(scenario())


def test_retries_stop_at_configured_limit_even_when_recovery_fails(monkeypatch):
    async def scenario():
        probes, requests = [], []
        async def show(self, model):
            return {'capabilities': ['embedding']}
        async def embed(self, **kwargs):
            if kwargs['input'] == HEALTH_CHECK_TEXT:
                probes.append(True)
            else:
                requests.append(True)
            raise httpx.ConnectError('still offline')
        monkeypatch.setattr(AsyncClient, 'show', show)
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=[HOSTS[0]], quarantine_seconds=0, retry_backoff=0)
        try:
            with pytest.raises(RuntimeError, match='3 attempts'):
                await pool.embed(model=MODEL, input='document')
            assert len(probes) == 2 and len(requests) == 1
            snapshot = pool.status_snapshot()
            assert snapshot['failed'] == 3 and snapshot['retries'] == 2
            assert snapshot['servers'][0]['health_failures'] == 2
            assert pool._available == [1] and pool._health_inflight == 0
        finally:
            await pool.aclose()
    asyncio.run(scenario())


def test_local_queue_wait_does_not_use_inference_timeout(monkeypatch):
    async def scenario():
        async def embed(self, **kwargs):
            await asyncio.sleep(0.015)
            return kwargs['input']
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=[HOSTS[0]], request_timeout=0.1, request_retries=0)
        try:
            # The final request waits > 0.1s, but its own inference is short.
            assert await asyncio.gather(*(pool.embed(model=MODEL, input=i) for i in range(12))) == list(range(12))
            assert pool.status_snapshot()['failed'] == 0
        finally:
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('response', [{}, {'embeddings': []}, {'embeddings': [[0.0, 0.0]]},
    {'embeddings': [[float('nan'), 1.0]]}, {'embeddings': [['bad']]}, {'embeddings': [[True]]}])
def test_preflight_rejects_unusable_vectors_before_document_requests(monkeypatch, response):
    async def scenario():
        async def show(self, model):
            return {'capabilities': ['embedding']}
        async def embed(self, **kwargs):
            assert kwargs['input'] == HEALTH_CHECK_TEXT
            return response
        monkeypatch.setattr(AsyncClient, 'show', show)
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=[HOSTS[0]])
        try:
            with pytest.raises(RuntimeError, match='preflight failed'):
                await pool.check_models(MODEL)
            assert pool.server_snapshots()[0]['health'] == 'quarantined'
            assert pool.server_snapshots()[0]['health_failures'] == 1
        finally:
            await pool.aclose()
    asyncio.run(scenario())


def test_only_one_recovery_probe_runs_before_reopening_capacity(monkeypatch):
    async def scenario():
        probing, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def show(self, model):
            return {'capabilities': ['embedding']}
        async def embed(self, **kwargs):
            calls.append(kwargs['input'])
            if kwargs['input'] == HEALTH_CHECK_TEXT:
                probing.set()
                await release.wait()
            return {'embeddings': [[1.0, 2.0]]}
        monkeypatch.setattr(AsyncClient, 'show', show)
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=[HOSTS[0]], concurrency_per_server=4, quarantine_seconds=0)
        pool._quarantine(0, TimeoutError('earlier failure'))
        tasks = [asyncio.create_task(pool.embed(model=MODEL, input=f'doc{i}')) for i in range(8)]
        try:
            await asyncio.wait_for(probing.wait(), 1)
            await asyncio.sleep(0)
            assert calls == [HEALTH_CHECK_TEXT]
            assert pool.status_snapshot()['running'] == 1
            release.set()
            await asyncio.gather(*tasks)
            assert calls.count(HEALTH_CHECK_TEXT) == 1
            assert pool._available == [4]
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['quarantine', 'backoff', 'health'])
def test_cancellation_during_retry_and_health_does_not_leak_capacity(monkeypatch, stage):
    async def scenario():
        reached = asyncio.Event()
        async def show(self, model):
            reached.set()
            await asyncio.Future()
        async def embed(self, **kwargs):
            raise TimeoutError('retry me')
        monkeypatch.setattr(AsyncClient, 'show', show)
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=[HOSTS[0]],
            quarantine_seconds=30 if stage == 'quarantine' else 0,
            retry_backoff=30 if stage == 'backoff' else 0)
        task = asyncio.create_task(pool.embed(model=MODEL, input='document'))
        try:
            if stage == 'health':
                await asyncio.wait_for(reached.wait(), 1)
            else:
                while not pool.status_snapshot()['retries']:
                    await asyncio.sleep(0)
                await asyncio.sleep(0)
            with pytest.raises(RuntimeError, match='active'):
                await pool.aclose()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pool._available == [1]
            assert pool._inflight == pool._health_inflight == 0
            assert pool.status_snapshot()['running'] == pool.status_snapshot()['queued'] == 0
            assert pool.status_snapshot()['cancelled'] == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await pool.aclose()
    asyncio.run(scenario())


def test_preflight_cancellation_awaits_probes_before_close(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        async def show(self, model):
            started.set()
            await asyncio.Future()
        monkeypatch.setattr(AsyncClient, 'show', show)
        pool = OllamaEmbeddingPool(hosts=HOSTS)
        task = asyncio.create_task(pool.check_models(MODEL))
        try:
            await started.wait()
            with pytest.raises(RuntimeError, match='active'):
                await pool.aclose()
            with pytest.raises(RuntimeError, match='preflight'):
                await pool.embed(model=MODEL, input='too early')
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert pool._health_inflight == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await pool.aclose()
    asyncio.run(scenario())


def test_late_success_does_not_reopen_a_host_after_another_request_fails(monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def embed(self, **kwargs):
            if kwargs['input'] == 'slow':
                started.set()
                await release.wait()
                return 'old success'
            raise TimeoutError('new failure')
        monkeypatch.setattr(AsyncClient, 'embed', embed)
        pool = OllamaEmbeddingPool(hosts=[HOSTS[0]], concurrency_per_server=2, request_retries=0)
        task = asyncio.create_task(pool.embed(model=MODEL, input='slow'))
        try:
            await started.wait()
            with pytest.raises(TimeoutError):
                await pool.embed(model=MODEL, input='failure')
            release.set()
            assert await task == 'old success'
            assert pool.server_snapshots()[0]['health'] == 'quarantined'
            assert pool._available == [2]
        finally:
            release.set()
            await task
            await pool.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('options', [dict(request_retries=-1), dict(request_retries=True),
    dict(quarantine_seconds=-1), dict(quarantine_seconds=float('inf')), dict(health_timeout=0),
    dict(health_timeout=float('nan')), dict(retry_backoff=-1)])
def test_invalid_resilience_settings(options):
    with pytest.raises(ValueError):
        OllamaEmbeddingPool(hosts=HOSTS, **options)
