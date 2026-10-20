"""Application helpers for the summarization notebook and its offline checks.

Importing this module does not import DOM modules or start model requests.
"""

import asyncio
from contextvars import ContextVar
import math
import hashlib
import json
from pathlib import Path
import tempfile
import os
from statistics import mean
import sys
from time import perf_counter
from unittest.mock import patch

from httpx import TimeoutException as HTTPTimeoutError
from ollama import AsyncClient
from tqdm.auto import tqdm


_document_id = ContextVar("summary_document_id", default=None)
_batch_active = False


def configure_summary_limits(summary_module="natural_text"):
    """Set notebook defaults before DOM import; reject stale kernel settings."""
    if summary_module not in {"natural_text", "object"}:
        raise ValueError("summary_module must be 'natural_text' or 'object'")
    defaults = {
        "RIBOSOME_DOCUMENT_CONCURRENCY": 4,
        "RIBOSOME_SUMMARY_CONCURRENCY": 8,
        "RIBOSOME_SUMMARY_TASK_LIMIT": 32,
    }
    limits = {}
    for name, default in defaults.items():
        value = os.environ.setdefault(name, str(default))
        try:
            parsed = int(value)
            if parsed < 1 or str(parsed) != value.strip():
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a positive integer; got {value!r}") from None
        limits[name] = parsed
    loaded = sys.modules.get(f"ribosome.core.dom.summary.{summary_module}")
    if loaded is not None:
        for name, attribute in (
            ("RIBOSOME_SUMMARY_CONCURRENCY", "_SUMMARY_CONCURRENCY"),
            ("RIBOSOME_SUMMARY_TASK_LIMIT", "_SUMMARY_TASK_LIMIT"),
        ):
            if getattr(loaded, attribute) != limits[name]:
                raise RuntimeError(
                    f"{name} differs from the imported summarizer. "
                    "Restart the kernel and run cells in order."
                )
    return limits


class RequestMetrics:
    """Record client latency separately from provider timing (nanoseconds)."""

    def __init__(self):
        self.records = []
        self.active = 0
        self.peak = 0
        self.document_active = {}
        self.document_peak = {}

    def start(self, document):
        self.active += 1
        self.peak = max(self.peak, self.active)
        active = self.document_active.get(document, 0) + 1
        self.document_active[document] = active
        self.document_peak[document] = max(self.document_peak.get(document, 0), active)

    def finish(self, document, started, response, status):
        record = {"document": document, "latency": perf_counter() - started, "status": status}
        for name in ("load_duration", "prompt_eval_duration", "eval_duration",
                     "prompt_eval_count", "eval_count"):
            value = response.get(name) if isinstance(response, dict) else getattr(response, name, None)
            if value is not None:
                record[name] = value
        self.records.append(record)
        self.active -= 1
        self.document_active[document] -= 1

    def snapshot(self, *, document=None, wall_seconds=None):
        records = self.records if document is None else [
            record for record in self.records if record["document"] == document]
        latencies = sorted(record["latency"] for record in records)
        metrics = {
            "request_count": len(records),
            "request_errors": sum(record["status"] == "error" for record in records),
            "cancelled_requests": sum(record["status"] == "cancelled" for record in records),
            "peak_requests": self.peak if document is None else self.document_peak.get(document, 0),
            "mean_latency_seconds": mean(latencies) if latencies else 0.0,
            "p95_latency_seconds": latencies[max(0, (95 * len(latencies) + 99) // 100 - 1)] if latencies else 0.0,
        }
        for field, name, divisor in (
            ("load_duration", "load_seconds", 1e9),
            ("prompt_eval_duration", "prompt_eval_seconds", 1e9),
            ("eval_duration", "generation_seconds", 1e9),
            ("prompt_eval_count", "prompt_tokens", 1),
            ("eval_count", "generated_tokens", 1),
        ):
            values = [record[field] for record in records if field in record]
            metrics[name] = sum(values) / divisor if values else None
        if wall_seconds is not None:
            metrics["requests_per_second"] = len(records) / wall_seconds if wall_seconds > 0 else 0.0
        return metrics


class SummaryAsyncClient(AsyncClient):
    """Shared Ollama client with metrics for non-streaming summary requests."""

    def __init__(self, *args, request_timeout=None, max_tokens=None, think=None, **kwargs):
        if request_timeout is not None and (
            isinstance(request_timeout, bool) or not isinstance(request_timeout, (int, float))
            or not math.isfinite(request_timeout) or request_timeout <= 0
        ):
            raise ValueError("request_timeout must be finite and positive")
        if max_tokens is not None and (
            isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1
        ):
            raise ValueError("max_tokens must be a positive integer")
        if request_timeout is not None:
            kwargs.setdefault("timeout", request_timeout)
        super().__init__(*args, **kwargs)
        self.request_timeout, self.max_tokens, self.think = request_timeout, max_tokens, think
        self.active_requests = {}
        self.metrics = RequestMetrics()

    def reset_metrics(self):
        if self.metrics.active:
            raise RuntimeError("Cannot reset metrics while requests are active")
        self.metrics = RequestMetrics()

    async def chat(self, *args, **kwargs):
        if kwargs.get("stream", False):
            raise ValueError("SummaryAsyncClient metrics require stream=False")
        if self.think is not None:
            kwargs.setdefault("think", self.think)
        if self.max_tokens is not None:
            options = kwargs.get("options") or {}
            options = options.model_dump(exclude_none=True) if hasattr(options, "model_dump") else dict(options)
            options.setdefault("num_predict", self.max_tokens)
            kwargs["options"] = options
        document = _document_id.get()
        started = perf_counter()
        identity = object()
        self.active_requests[identity] = (document, started)
        response = None
        status = "error"
        self.metrics.start(document)
        try:
            async with asyncio.timeout(self.request_timeout):
                response = await self._chat_request(*args, **kwargs)
            status = "completed"
            return response
        except (TimeoutError, HTTPTimeoutError) as exc:
            raise TimeoutError(f"Ollama summary request timed out for {document or 'unassigned document'}; "
                               "check server load or RIBOSOME_SUMMARY_REQUEST_TIMEOUT") from exc
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            self.active_requests.pop(identity, None)
            self.metrics.finish(document, started, response, status)

    async def _chat_request(self, *args, **kwargs):
        """Transport hook; subclasses retain common limits and document metrics."""
        return await super().chat(*args, **kwargs)


def count_summary_nodes(value):
    """Count typed nodes visited by textualize, excluding skipped internals."""
    if isinstance(value, list):
        return sum(count_summary_nodes(item) for item in value)
    if not isinstance(value, dict):
        return 0
    if value.get("t") in {"Image", "Cite", "AlignDefault", "ColWidth"}:
        return 1
    return int("t" in value) + sum(
        count_summary_nodes(child) for key, child in value.items() if key not in {"t", "s"})


async def _setup_document(dom):
    """Do not detach an uninterruptible setup thread when its worker is cancelled."""
    setup = asyncio.create_task(asyncio.to_thread(dom.setup))
    try:
        await asyncio.shield(setup)
    except asyncio.CancelledError:
        while not setup.done():
            try:
                await asyncio.shield(setup)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not setup.cancelled():
            setup.exception()  # Retrieve failures; preserve the original cancellation.
        raise


class SummaryCheckpoint:
    """Atomic document-level progress; only unchanged successes are reusable."""

    def __init__(self, path, model, host, resume, *, settings=None):
        self.path = Path(path)
        self.config = {"model": model, "host": host}
        if settings is not None:
            self.config["settings"] = settings
        self.entries = {}
        if resume and self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("version") != 1:
                raise ValueError(f"Unsupported summary checkpoint: {self.path}")
            if saved.get("config") == self.config:
                self.entries = saved["entries"]

    @staticmethod
    def signature(file):
        paths = [file, file.with_name(f"{file.stem}_ast.json"),
                 file.with_name(f"{file.stem}_semantics.json")]
        return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file() else None for path in paths}

    def completed(self, file):
        entry = self.entries.get(str(file.resolve()))
        if entry and entry["row"]["status"] == "completed" \
                and entry["signature"] == self.signature(file) \
                and file.with_name(f"{file.stem}_semantics.json").is_file():
            return dict(entry["row"], file=str(file), resumed=True)

    def record(self, file, row):
        self.entries[str(file.resolve())] = {
            "row": row, "signature": self.signature(file)}
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": 1, "config": self.config, "entries": self.entries}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


async def summarize_documents(files, client, model, database, *, concurrency=4,
                              progress_interval=0.25, dom_factory=None, progress_factory=tqdm,
                              checkpoint_path=None, resume=True, checkpoint_host=None):
    """Summarize with bounded workers and optional atomic per-document checkpoints.

    Resume retries failed/incomplete documents and skips unchanged successes.
    Set resume=False to start a fresh checkpoint and regenerate all summaries.
    """
    import json
    from pathlib import Path
    from ribosome.core.dom import model as dom_model
    from ribosome.core.dom.summary import natural_text as dom_summary

    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if isinstance(progress_interval, bool) or not isinstance(progress_interval, (int, float)) \
            or not math.isfinite(progress_interval) or progress_interval <= 0:
        raise ValueError("progress_interval must be finite and positive")
    global _batch_active
    if _batch_active:
        raise RuntimeError("A summarization batch is already running; await it before starting another")
    files = [Path(file) for file in files]
    if not files:
        return []
    dom_factory = dom_factory or dom_model.DOMClass
    results = [None] * len(files)
    checkpoint = (SummaryCheckpoint(checkpoint_path, model, checkpoint_host, resume)
                  if checkpoint_path is not None else None)
    if checkpoint:
        for index, file in enumerate(files):
            results[index] = checkpoint.completed(file)
    pending = iter((index, file) for index, file in enumerate(files) if results[index] is None)
    summarize_node = dom_summary.summarize_node_async
    document_progress = ContextVar("document_progress", default=None)
    active = {}
    totals = {"nodes": 0, "done_nodes": 0, "documents": sum(row is not None for row in results), "status": "starting"}
    _batch_active = True
    try:
        with progress_factory(total=len(files), desc="Documents", unit="doc", position=0) as documents, \
             progress_factory(total=0, desc="Nodes (active documents)", unit="node", position=1) as nodes:

            def render():
                nodes.total = totals["nodes"]
                nodes.n = totals["done_nodes"]
                nodes.set_postfix_str(" | ".join(
                    f"{state['name']}: {state['done']}/{state['total']}" for state in active.values()),
                    refresh=False)
                documents.n = totals["documents"]
                documents.set_postfix(status=totals["status"], active=len(active), refresh=False)
                nodes.refresh()
                documents.refresh()

            async def renderer():
                while True:
                    await asyncio.sleep(progress_interval)
                    render()

            async def summarize_with_progress(node, *args, **kwargs):
                result = await summarize_node(node, *args, **kwargs)
                state = document_progress.get()
                if state is not None and isinstance(node, dict) and "t" in node:
                    state["done"] += 1
                    totals["done_nodes"] += 1
                return result

            async def process_document(index, md_file):
                start = perf_counter()
                row = {"file": str(md_file), "status": "failed",
                       "output": str(md_file.with_name(f"{md_file.stem}_semantics.json")), "error": ""}
                token = None
                metrics_token = _document_id.set(str(md_file))
                try:
                    dom = dom_factory(md_file, ollama_client=client, ollama_model=model, db_client=database)
                    await _setup_document(dom)
                    if not dom.ast_json:
                        raise ValueError("Empty Markdown document: no AST to summarize")
                    state = {"name": md_file.name, "done": 0,
                             "total": count_summary_nodes(json.loads(dom.ast_json)["blocks"]) + 1}
                    active[index] = state
                    token = document_progress.set(state)
                    totals["nodes"] += state["total"]
                    await dom.textualize()
                    if dom.semantics_json is None:
                        raise ValueError("Summarization returned no semantics JSON")
                    json.loads(dom.semantics_json)
                    state["done"] += 1
                    totals["done_nodes"] += 1
                    row["status"] = "completed"
                except asyncio.CancelledError:
                    progress_factory.write(f"Cancelled while processing: {md_file}")
                    raise
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    progress_factory.write(f"FAILED {md_file}: {row['error']}")
                finally:
                    _document_id.reset(metrics_token)
                    if token is not None:
                        document_progress.reset(token)
                        state = active.pop(index)
                        totals["nodes"] -= state["total"] - state["done"]
                row["elapsed_seconds"] = round(perf_counter() - start, 3)
                if isinstance(client, SummaryAsyncClient):
                    row.update(client.metrics.snapshot(document=str(md_file)))
                row["resumed"] = False
                if checkpoint:
                    checkpoint.record(md_file, row)
                results[index] = row
                totals["documents"] += 1
                totals["status"] = row["status"]

            async def worker():
                for index, md_file in pending:
                    await process_document(index, md_file)

            with patch.object(dom_model, "summarize_node_async", summarize_with_progress), \
                 patch.object(dom_summary, "summarize_node_async", summarize_with_progress):
                tasks = []
                display_task = asyncio.create_task(renderer())
                try:
                    render()
                    tasks = [asyncio.create_task(worker()) for _ in range(min(concurrency, len(files)))]
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    display_task.cancel()
                    await asyncio.gather(display_task, return_exceptions=True)
                    render()
    finally:
        _batch_active = False
        if checkpoint:
            checkpoint.save()
    return results
