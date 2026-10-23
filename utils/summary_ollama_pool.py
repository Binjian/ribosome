"""Bounded async Ollama routing for the object-summary application."""

import asyncio
import logging
from time import perf_counter
from urllib.parse import urlsplit

from ollama import AsyncClient
from httpx import TimeoutException as HTTPTimeoutError

from utils.summary_asyncio import RequestMetrics, SummaryAsyncClient, _document_id


logger = logging.getLogger(__name__)


class OllamaSummaryPool(SummaryAsyncClient):
    """Route each request to an available server; faster servers free slots sooner.

    Inherits aggregate/document metrics and generation/deadline controls. Transient
    timeouts retry with bounded backoff after returning their server slot. Other
    failures retain atomic document failure semantics. Requests are always awaited.
    All servers must pass preflight before processing a corpus.
    """

    def __init__(self, *, hosts, concurrency_per_server=4, headers=None,
                 request_timeout=300.0, max_tokens=2048, think=False, request_retries=2):
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
        if isinstance(request_retries, bool) or not isinstance(request_retries, int) or request_retries < 0:
            raise ValueError('request_retries must be a non-negative integer')
        super().__init__(host=hosts[0], headers=headers, request_timeout=request_timeout,
                         max_tokens=max_tokens, think=think)
        self.hosts = hosts
        self.concurrency_per_server = concurrency_per_server
        self.request_retries = request_retries
        self.servers = [AsyncClient(host=host, headers=headers, timeout=request_timeout) for host in hosts]
        self.server_metrics = [RequestMetrics() for _ in hosts]
        self._slots = asyncio.Queue()
        # Interleave slots so an idle pool initially sends work to every server.
        for _ in range(concurrency_per_server):
            for index in range(len(hosts)):
                self._slots.put_nowait(index)
        self._closed = False
        self._pending_requests = 0

    def reset_metrics(self):
        if self._pending_requests:
            raise RuntimeError('Cannot reset metrics while requests are active')
        super().reset_metrics()
        self.server_metrics = [RequestMetrics() for _ in self.hosts]

    async def chat(self, *args, **kwargs):
        if self._closed:
            raise RuntimeError('Ollama summary pool is closed; rerun notebook setup')
        self._pending_requests += 1
        try:
            for attempt in range(self.request_retries + 1):
                try:
                    return await super().chat(*args, **kwargs)
                except TimeoutError as exc:
                    if attempt == self.request_retries:
                        raise TimeoutError(f'{exc}; exhausted {attempt + 1} attempt(s)') from exc
                    delay = min(2 ** attempt, 30)
                    logger.warning('%s. Retrying summary (%s/%s) in %ss',
                                   exc, attempt + 1, self.request_retries, delay)
                    await asyncio.sleep(delay)
        finally:
            self._pending_requests -= 1

    async def _chat_with_timeout(self, *args, **kwargs):
        if self._closed:
            raise RuntimeError('Ollama summary pool is closed; rerun notebook setup')
        index = await self._slots.get()
        metrics = self.server_metrics[index]
        document, started = _document_id.get(), perf_counter()
        response, status = None, 'error'
        metrics.start(document)
        try:
            # Local pool waiting must not consume the provider's inference budget.
            # Server-side queue time is still covered by this deadline.
            async with asyncio.timeout(self.request_timeout):
                response = await self.servers[index].chat(*args, **kwargs)
            status = 'completed'
            return response
        except asyncio.CancelledError:
            status = 'cancelled'
            raise
        except (TimeoutError, HTTPTimeoutError) as exc:
            raise TimeoutError(f'Ollama server {self.hosts[index]}') from exc
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
        if self._pending_requests:
            raise RuntimeError('Cancel and await active summaries before closing the pool')
        self._closed = True
        await asyncio.gather(self._client.aclose(), *(server._client.aclose() for server in self.servers))
