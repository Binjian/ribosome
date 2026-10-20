"""Bounded async Ollama routing for the object-summary application."""

import asyncio
from time import perf_counter
from urllib.parse import urlsplit

from ollama import AsyncClient

from utils.summary_asyncio import RequestMetrics, SummaryAsyncClient, _document_id


class OllamaSummaryPool(SummaryAsyncClient):
    """Route each request to an available server; faster servers free slots sooner.

    Inherits aggregate/document metrics and generation/deadline controls. Requests
    are never detached or retried: provider failures retain atomic document failure
    semantics. All servers must pass preflight before processing a corpus.
    """

    def __init__(self, *, hosts, concurrency_per_server=4, headers=None,
                 request_timeout=300.0, max_tokens=2048, think=False):
        hosts = [host.strip().rstrip('/') for host in hosts]
        if not hosts or len(set(hosts)) != len(hosts):
            raise ValueError('hosts must contain distinct Ollama server URLs')
        for host in hosts:
            url = urlsplit(host)
            if url.scheme not in {'http', 'https'} or not url.netloc:
                raise ValueError(f'Invalid Ollama server URL: {host!r}')
        if isinstance(concurrency_per_server, bool) or not isinstance(concurrency_per_server, int) \
                or concurrency_per_server < 1:
            raise ValueError('concurrency_per_server must be a positive integer')
        super().__init__(host=hosts[0], headers=headers, request_timeout=request_timeout,
                         max_tokens=max_tokens, think=think)
        self.hosts = hosts
        self.concurrency_per_server = concurrency_per_server
        self.servers = [AsyncClient(host=host, headers=headers, timeout=request_timeout) for host in hosts]
        self.server_metrics = [RequestMetrics() for _ in hosts]
        self._slots = asyncio.Queue()
        # Interleave slots so an idle pool initially sends work to every server.
        for _ in range(concurrency_per_server):
            for index in range(len(hosts)):
                self._slots.put_nowait(index)
        self._closed = False

    def reset_metrics(self):
        super().reset_metrics()
        self.server_metrics = [RequestMetrics() for _ in self.hosts]

    async def _chat_request(self, *args, **kwargs):
        if self._closed:
            raise RuntimeError('Ollama summary pool is closed; rerun notebook setup')
        index = await self._slots.get()
        metrics = self.server_metrics[index]
        document, started = _document_id.get(), perf_counter()
        response, status = None, 'error'
        metrics.start(document)
        try:
            response = await self.servers[index].chat(*args, **kwargs)
            status = 'completed'
            return response
        except asyncio.CancelledError:
            status = 'cancelled'
            raise
        except Exception as exc:
            raise RuntimeError(f'Ollama server {self.hosts[index]}: {exc}') from exc
        finally:
            metrics.finish(document, started, response, status)
            self._slots.put_nowait(index)

    async def check_models(self, model):
        """Check every server concurrently, without loading a model or inferring."""
        async def check(index):
            async with asyncio.timeout(min(self.request_timeout or 30, 30)):
                response = await self.servers[index].list()
            available = {item.model for item in response.models}
            if model not in available:
                raise ValueError(f'Model {model!r} unavailable; installed: {sorted(available)}')
        results = await asyncio.gather(*(check(i) for i in range(len(self.hosts))), return_exceptions=True)
        errors = [f'{host}: {result}' for host, result in zip(self.hosts, results)
                  if isinstance(result, BaseException)]
        if errors:
            raise RuntimeError('Ollama preflight failed: ' + '; '.join(errors))
        return model

    def server_status(self):
        return ' | '.join(f'{host}: {metrics.active}/{self.concurrency_per_server}'
                          for host, metrics in zip(self.hosts, self.server_metrics))

    def server_snapshots(self, *, wall_seconds=None):
        return [dict(host=host, **metrics.snapshot(wall_seconds=wall_seconds))
                for host, metrics in zip(self.hosts, self.server_metrics)]

    async def aclose(self):
        """Close idle HTTP clients when finished using the notebook pool."""
        if self.metrics.active:
            raise RuntimeError('Cancel and await active summaries before closing the pool')
        self._closed = True
        await asyncio.gather(self._client.aclose(), *(server._client.aclose() for server in self.servers))
