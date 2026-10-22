"""Bounded asynchronous Ollama embedding requests across multiple servers."""

import asyncio
import math
from time import perf_counter
from urllib.parse import urlsplit

from ollama import AsyncClient


class OllamaEmbeddingPool:
    """Route to available server slots; preserve provider responses and cancellation.

    All servers must have the same embedding model installed. Preflight only
    inspects model metadata; it does not pull models or request embeddings.
    """

    def __init__(self, *, hosts, concurrency_per_server=1, headers=None, request_timeout=300.0):
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
        self.hosts = hosts
        self.concurrency_per_server = concurrency_per_server
        self.request_timeout = request_timeout
        self.servers = [AsyncClient(host=host, headers=headers, timeout=request_timeout) for host in hosts]
        self._slots = asyncio.Queue()
        for _ in range(concurrency_per_server):
            for i in range(len(hosts)):
                self._slots.put_nowait(i)
        self._closed = False
        self._inflight = 0
        self._requests = {}
        self._outcomes = dict(completed=0, failed=0, cancelled=0)
        self.metrics = [dict(host=host, active=0, completed=0, failed=0, cancelled=0) for host in hosts]

    async def embed(self, *, model, input, **kwargs):
        if self._closed:
            raise RuntimeError('Ollama embedding pool is closed; rerun preflight')
        self._inflight += 1
        token = object()
        self._requests[token] = (None, perf_counter())
        index = None
        try:
            # Include waiting for a server slot in the request deadline.
            async with asyncio.timeout(self.request_timeout):
                index = await self._slots.get()
                self._requests[token] = (index, perf_counter())
                self.metrics[index]['active'] += 1
                response = await self.servers[index].embed(model=model, input=input, **kwargs)
                self.metrics[index]['completed'] += 1
                self._outcomes['completed'] += 1
                return response
        except asyncio.CancelledError:
            self._outcomes['cancelled'] += 1
            if index is not None:
                self.metrics[index]['cancelled'] += 1
            raise
        except Exception as exc:
            self._outcomes['failed'] += 1
            if index is not None:
                self.metrics[index]['failed'] += 1
            location = self.hosts[index] if index is not None else 'server queue'
            if isinstance(exc, TimeoutError):
                raise TimeoutError(f'Ollama embedding timeout at {location} after {self.request_timeout:g}s') from exc
            raise RuntimeError(f'Ollama embedding server {location}: {exc}') from exc
        finally:
            self._requests.pop(token, None)
            if index is not None:
                self.metrics[index]['active'] -= 1
                self._slots.put_nowait(index)
            self._inflight -= 1

    async def check_models(self, model):
        if self._closed:
            raise RuntimeError('Ollama embedding pool is closed; rerun preflight')

        async def check(server):
            async with asyncio.timeout(min(self.request_timeout, 30)):
                response = await server.show(model)
                capabilities = response.get('capabilities') if isinstance(response, dict) else getattr(response, 'capabilities', None)
                if capabilities is not None and 'embedding' not in capabilities:
                    raise ValueError(f'Model {model!r} does not report embedding support: {capabilities}')

        results = await asyncio.gather(*(check(server) for server in self.servers), return_exceptions=True)
        errors = [f'{host}: {result}' for host, result in zip(self.hosts, results) if isinstance(result, BaseException)]
        if errors:
            raise RuntimeError('Ollama embedding preflight failed: ' + '; '.join(errors))
        return model

    def server_snapshots(self):
        return [dict(row) for row in self.metrics]

    def status_snapshot(self):
        """Instantaneous HTTP request activity, including queued work and ages."""
        now = perf_counter()
        requests = list(self._requests.values())
        servers = []
        for index, row in enumerate(self.metrics):
            ages = [now - started for server, started in requests if server == index]
            servers.append(dict(row, capacity=self.concurrency_per_server,
                                oldest_seconds=max(ages, default=0.0)))
        running = sum(row['active'] for row in servers)
        return dict(running=running, queued=sum(server is None for server, _ in requests),
                    capacity=len(self.hosts) * self.concurrency_per_server,
                    oldest_seconds=max((now - started for _, started in requests), default=0.0),
                    **self._outcomes, servers=servers)

    async def aclose(self):
        if self._inflight:
            raise RuntimeError('Cancel and await active embedding requests before closing the pool')
        self._closed = True
        await asyncio.gather(*(server.close() for server in self.servers))

    async def close(self):
        await self.aclose()
