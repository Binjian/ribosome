"""Bounded asynchronous Ollama embedding requests across multiple servers."""

import asyncio
import logging
import math
from time import perf_counter
from urllib.parse import urlsplit

import httpx
from ollama import AsyncClient

HEALTH_CHECK_TEXT = 'Ribosome embedding health check.'
logger = logging.getLogger(__name__)


class _HealthCheckError(ConnectionError):
    """A quarantined server has not demonstrated that inference is working."""


class OllamaEmbeddingPool:
    """Route to available server slots; preserve provider responses and cancellation.

    Preflight and recovery check metadata and a small synthetic embedding.
    Transient failures quarantine a server without changing configured identity.
    Request deadlines start after local capacity is acquired. Retries are bounded;
    with one host they wait for quarantine and a successful recovery probe.
    """

    def __init__(self, *, hosts, concurrency_per_server=1, headers=None, request_timeout=300.0,
                 request_retries=2, quarantine_seconds=30.0, health_timeout=60.0, retry_backoff=1.0):
        if isinstance(hosts, str):
            raise ValueError('hosts must be a sequence of distinct server URLs')
        hosts = [host.strip().rstrip('/') for host in hosts]
        if not hosts or len(set(hosts)) != len(hosts):
            raise ValueError('hosts must contain distinct server URLs')
        for host in hosts:
            parsed = urlsplit(host)
            if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
                raise ValueError(f'Invalid Ollama server URL: {host!r}')
        if isinstance(concurrency_per_server, bool) or not isinstance(concurrency_per_server, int) or concurrency_per_server < 1:
            raise ValueError('concurrency_per_server must be a positive integer')
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError('request_timeout must be finite and positive')
        if isinstance(request_retries, bool) or not isinstance(request_retries, int) or request_retries < 0:
            raise ValueError('request_retries must be a nonnegative integer')
        for name, value in [('quarantine_seconds', quarantine_seconds), ('retry_backoff', retry_backoff)]:
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'{name} must be finite and nonnegative')
        if not math.isfinite(health_timeout) or health_timeout <= 0:
            raise ValueError('health_timeout must be finite and positive')
        self.hosts = hosts
        self.concurrency_per_server = concurrency_per_server
        self.request_timeout = request_timeout
        self.request_retries = request_retries
        self.quarantine_seconds = quarantine_seconds
        self.health_timeout = health_timeout
        self.retry_backoff = retry_backoff
        self.servers = [AsyncClient(host=host, headers=headers, timeout=request_timeout) for host in hosts]
        self._available = [concurrency_per_server] * len(hosts)
        self._healthy = [True] * len(hosts)
        self._quarantined_until = [0.0] * len(hosts)
        self._recovering = [False] * len(hosts)
        self._epochs = [0] * len(hosts)
        self._changed = asyncio.Event()
        self._cursor = 0
        self._closed = False
        self._inflight = 0
        self._health_inflight = 0
        self._checking = False
        self._requests = {}
        self._outcomes = dict(completed=0, failed=0, cancelled=0, retries=0)
        self.metrics = [dict(host=host, active=0, completed=0, failed=0, cancelled=0,
                             retries=0, health_checks=0, health_failures=0, last_error='') for host in hosts]

    def _quarantine(self, index, exc):
        self._healthy[index] = False
        self._epochs[index] += 1
        self._quarantined_until[index] = perf_counter() + self.quarantine_seconds
        self.metrics[index]['last_error'] = f'{type(exc).__name__}: {exc}'[:500]
        self._changed.set()
        logger.warning('Ollama server %s quarantined for %gs: %s',
                       self.hosts[index], self.quarantine_seconds, self.metrics[index]['last_error'])

    def _mark_healthy(self, index):
        self._healthy[index] = True
        self._quarantined_until[index] = 0.0
        self.metrics[index]['last_error'] = ''
        self._changed.set()

    async def _take_slot(self, previous):
        while True:
            self._changed.clear()
            now = perf_counter()
            order = [(self._cursor + offset) % len(self.hosts) for offset in range(len(self.hosts))]
            if previous in order:
                order.remove(previous)
                order.append(previous)
            for index in order:
                if not self._available[index]:
                    continue
                recovery = not self._healthy[index]
                # One recovery probe is allowed after older requests have drained.
                if recovery and (self._quarantined_until[index] > now or
                                 self.metrics[index]['active'] or self._recovering[index]):
                    continue
                self._available[index] -= 1
                self.metrics[index]['active'] += 1
                self._recovering[index] = recovery
                self._cursor = (index + 1) % len(self.hosts)
                return index, self._epochs[index], recovery
            delays = [until - now for i, until in enumerate(self._quarantined_until)
                      if not self._healthy[i] and not self.metrics[i]['active'] and
                      not self._recovering[i] and until > now]
            if delays:
                try:
                    await asyncio.wait_for(self._changed.wait(), min(delays))
                except TimeoutError:
                    pass
            else:
                await self._changed.wait()

    async def _probe_server(self, index, model):
        self._health_inflight += 1
        self.metrics[index]['health_checks'] += 1
        try:
            async with asyncio.timeout(self.health_timeout):
                response = await self.servers[index].show(model)
                capabilities = response.get('capabilities') if isinstance(response, dict) else getattr(response, 'capabilities', None)
                if capabilities is not None and 'embedding' not in capabilities:
                    raise ValueError(f'Model {model!r} does not report embedding support: {capabilities}')
                response = await self.servers[index].embed(model=model, input=HEALTH_CHECK_TEXT)
                vectors = response.get('embeddings', response.get('embedding')) if isinstance(response, dict) else getattr(response, 'embeddings', None)
                if vectors is None:
                    vectors = getattr(response, 'embedding', None)
                vector = vectors[0] if vectors and isinstance(vectors[0], (list, tuple)) else vectors
                if not vector or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vector) or not any(vector):
                    raise ValueError('Health check returned no usable embedding vector')
        except TimeoutError as exc:
            self.metrics[index]['health_failures'] += 1
            raise TimeoutError(f'Ollama embedding health check timeout at {self.hosts[index]} after {self.health_timeout:g}s') from exc
        except Exception:
            self.metrics[index]['health_failures'] += 1
            raise
        finally:
            self._health_inflight -= 1

    @staticmethod
    def _transient(exc):
        return (isinstance(exc, (TimeoutError, httpx.TransportError, ConnectionError)) or
                getattr(exc, 'status_code', None) in {408, 429, 500, 502, 503, 504})

    async def embed(self, *, model, input, **kwargs):
        if self._closed:
            raise RuntimeError('Ollama embedding pool is closed; rerun preflight')
        if self._checking:
            raise RuntimeError('Await embedding preflight before starting requests')
        self._inflight += 1
        token = object()
        self._requests[token] = (None, perf_counter())
        index = None
        try:
            previous = None
            for attempt in range(self.request_retries + 1):
                index, epoch, recovery = await self._take_slot(previous)
                self._requests[token] = (index, perf_counter())
                try:
                    if recovery:
                        try:
                            await self._probe_server(index, model)
                        except Exception as exc:
                            raise _HealthCheckError(f'Recovery embedding probe failed: {exc}') from exc
                    async with asyncio.timeout(self.request_timeout):
                        response = await self.servers[index].embed(model=model, input=input, **kwargs)
                    # A success from an older request must not undo a newer failure.
                    if epoch == self._epochs[index]:
                        self._mark_healthy(index)
                    self.metrics[index]['completed'] += 1
                    self._outcomes['completed'] += 1
                    return response
                except asyncio.CancelledError:
                    self.metrics[index]['cancelled'] += 1
                    raise
                except Exception as exc:
                    self.metrics[index]['failed'] += 1
                    self._outcomes['failed'] += 1
                    transient = self._transient(exc)
                    if transient:
                        self._quarantine(index, exc)
                    if not transient or attempt == self.request_retries:
                        location = self.hosts[index]
                        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                            raise TimeoutError(f'Ollama embedding timeout at {location} after {self.request_timeout:g}s per attempt ({attempt + 1} attempts)') from exc
                        raise RuntimeError(f'Ollama embedding server {location} ({attempt + 1} attempts): {exc}') from exc
                    self.metrics[index]['retries'] += 1
                    self._outcomes['retries'] += 1
                finally:
                    previous = index
                    self.metrics[index]['active'] -= 1
                    self._available[index] += 1
                    if recovery:
                        self._recovering[index] = False
                    index = None
                    self._requests[token] = (None, perf_counter())
                    self._changed.set()
                await asyncio.sleep(min(30.0, self.retry_backoff * 2 ** min(attempt, 10)))
        except asyncio.CancelledError:
            self._outcomes['cancelled'] += 1
            raise
        finally:
            self._requests.pop(token, None)
            self._inflight -= 1

    async def check_models(self, model):
        if self._closed:
            raise RuntimeError('Ollama embedding pool is closed; rerun preflight')
        if self._inflight or self._checking:
            raise RuntimeError('Await active embedding requests and preflight before checking models')
        self._checking = True
        try:
            results = await asyncio.gather(*(self._probe_server(i, model) for i in range(len(self.hosts))), return_exceptions=True)
            errors = []
            for index, result in enumerate(results):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, Exception):
                    self._quarantine(index, result)
                    errors.append(f'{self.hosts[index]}: {type(result).__name__}: {result}')
                else:
                    self._mark_healthy(index)
            if len(errors) == len(self.hosts):
                raise RuntimeError('Ollama embedding preflight failed: ' + '; '.join(errors))
        finally:
            self._checking = False
        return model

    def server_snapshots(self):
        now = perf_counter()
        return [dict(row, health='checking' if self._recovering[i] or self._checking else
                     'healthy' if self._healthy[i] else 'quarantined',
                     cooldown_seconds=max(0.0, self._quarantined_until[i] - now))
                for i, row in enumerate(self.metrics)]

    def status_snapshot(self):
        """Instantaneous HTTP request activity, including queued work and ages."""
        now = perf_counter()
        requests = list(self._requests.values())
        servers = []
        for index, row in enumerate(self.server_snapshots()):
            ages = [now - started for server, started in requests if server == index]
            servers.append(dict(row, capacity=self.concurrency_per_server,
                                oldest_seconds=max(ages, default=0.0)))
        running = sum(row['active'] for row in servers)
        return dict(running=running, queued=sum(server is None for server, _ in requests),
                    capacity=len(self.hosts) * self.concurrency_per_server,
                    healthy_capacity=sum(self.concurrency_per_server for healthy in self._healthy if healthy),
                    oldest_seconds=max((now - started for _, started in requests), default=0.0),
                    **self._outcomes, servers=servers)

    async def aclose(self):
        if self._inflight or self._health_inflight or self._checking:
            raise RuntimeError('Cancel and await active embedding requests before closing the pool')
        self._closed = True
        await asyncio.gather(*(server.close() for server in self.servers))

    async def close(self):
        await self.aclose()
