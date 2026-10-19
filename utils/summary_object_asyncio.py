"""Batch object-tree summarization for the operational notebook.

Importing this module does not import DOM modules or contact a model provider.
"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import math
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from tqdm.auto import tqdm

from utils.summary_asyncio import SummaryAsyncClient, SummaryCheckpoint, _document_id


_batch_active = False


def discover_markdown(input_dir):
    """Discover sorted Markdown paths recursively; reject missing/empty inputs."""
    input_dir = Path(input_dir).resolve()
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Markdown input directory does not exist: {input_dir}")
    files = sorted(path for path in input_dir.rglob("*")
                   if path.is_file() and path.suffix.lower() == ".md")
    if not files:
        raise ValueError(f"No Markdown files found under: {input_dir}")
    return files


async def check_model(client, model, host):
    """Verify the configured model is installed before starting a batch."""
    try:
        response = await client.list()
    except Exception as exc:
        raise RuntimeError(f"Cannot list Ollama models at {host}: {exc}") from exc
    available = {item.model for item in response.models}
    if model not in available:
        raise RuntimeError(f"Model {model!r} is unavailable at {host}; available: {sorted(available)}")
    return model


async def summarize_object_documents(
    files, client, model, *, corpus="", concurrency=4, progress_interval=0.25,
    checkpoint_path=None, resume=True, checkpoint_host=None, lang="zh",
    leaf_min_len=100, min_len=None, progress_factory=tqdm,
):
    """Summarize Markdown directly into atomic, recursively structured outputs.

    Results follow input order. A document is published only after its full tree
    succeeds. Failures are reported independently; cancellation awaits outstanding
    requests and saves completed-document checkpoints. AST caches are not written.
    Set resume=False after image or summarizer changes to rebuild every document.
    """
    from ribosome.core.dom.summary import object as dom_summary

    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    if isinstance(progress_interval, bool) or not isinstance(progress_interval, (int, float)) \
            or not math.isfinite(progress_interval) or progress_interval <= 0:
        raise ValueError("progress_interval must be finite and positive")
    global _batch_active
    if _batch_active:
        raise RuntimeError("An object summarization batch is already running; await it before starting another")
    files = [Path(file).resolve() for file in files]
    if not files:
        return []
    document_progress = ContextVar("object_document_progress", default=None)
    extract_tree = dom_summary.extract_markdown_object_tree
    summarize_object = dom_summary._summarize_markdown_object_async
    get_semaphore = dom_summary._get_summary_semaphore
    active = {}
    checkpoint = None
    image_token = None
    _batch_active = True
    try:
        snapshot = await dom_summary._prepare_ignored_images()
        image_token = dom_summary._ignored_images.set(snapshot)
        checkpoint = (SummaryCheckpoint(
            checkpoint_path, model, checkpoint_host, resume,
            settings={"summarizer": "object", "object_schema_version": 4, "lang": lang,
                      "leaf_min_len": leaf_min_len, "min_len": min_len,
                      "ignored_images_signature": snapshot.signature,
                      "generation": ({"think": client.think, "max_tokens": client.max_tokens}
                                     if isinstance(client, SummaryAsyncClient) else None)},
        ) if checkpoint_path is not None else None)
        results = [checkpoint.completed(file) if checkpoint else None for file in files]
        for row in results:
            if row is not None:
                row["corpus"] = str(corpus)
        pending = iter((index, file) for index, file in enumerate(files) if results[index] is None)
        durations = []
        resumed_count = sum(row is not None for row in results)
        request_offset = len(client.metrics.records) if isinstance(client, SummaryAsyncClient) else 0
        totals = {"objects": 0, "done_objects": 0,
                  "documents": sum(row is not None for row in results), "status": "starting"}
        with progress_factory(total=len(files), desc="Documents", unit="doc", position=0,
                              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}{postfix}]") as documents, \
             progress_factory(total=0, desc="Objects (active documents)", unit="object", position=1,
                              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}{postfix}]") as objects:

            def render():
                documents.n = totals["documents"]
                status = dict(status=totals["status"], active=len(active), resumed=resumed_count)
                if durations:
                    status.update(last_doc=f"{durations[-1]:.1f}s", mean_doc=f"{sum(durations) / len(durations):.1f}s")
                if isinstance(client, SummaryAsyncClient):
                    status.update(requests=len(client.metrics.records) - request_offset,
                                  in_flight=len(client.active_requests))
                    if client.active_requests:
                        oldest = min(started for _, started in client.active_requests.values())
                        status["oldest_request"] = f"{perf_counter() - oldest:.1f}s"
                documents.set_postfix(**status, refresh=False)
                objects.total = totals["objects"]
                objects.n = totals["done_objects"]
                objects.set_postfix_str(" | ".join(
                    f"{state['name']}: objects {state['done']}/{state['total']}, "
                    f"{state['phase']}, queued={state['queued']}, running={state['running']} "
                    f"({perf_counter() - state['started']:.0f}s)" for state in active.values()),
                    refresh=False)
                objects.refresh()
                documents.refresh()

            async def renderer():
                while True:
                    await asyncio.sleep(progress_interval)
                    render()

            @asynccontextmanager
            async def semaphore_with_progress():
                # Observe the existing shared limit without changing scheduling.
                state = document_progress.get()
                if state is None:
                    async with get_semaphore():
                        yield
                    return
                if dom_summary._summary_stage.get() == 'final document summary':
                    state['phase'] = 'final document summary'
                state['queued'] += 1
                try:
                    await get_semaphore().acquire()
                finally:
                    state['queued'] -= 1
                state['running'] += 1
                try:
                    yield
                finally:
                    state['running'] -= 1
                    get_semaphore().release()

            def extract_with_progress(file):
                tree = extract_tree(file)
                state = document_progress.get()
                if state is not None:
                    state["total"] = sum(1 for _ in dom_summary._object_nodes(tree))
                    totals["objects"] += state["total"]
                    state["phase"] = "summarizing tree"
                return tree

            async def summarize_with_progress(*args, **kwargs):
                result = await summarize_object(*args, **kwargs)
                state = document_progress.get()
                if state is not None:
                    state["done"] += 1
                    totals["done_objects"] += 1
                    if state["done"] == state["total"]:
                        state["phase"] = "objects done; prose/parents pending"
                return result

            async def process_document(index, file):
                started = perf_counter()
                output = file.with_name(f"{file.stem}_semantics.json")
                row = {"corpus": str(corpus), "file": str(file), "status": "failed",
                       "output": str(output), "error": "", "resumed": False}
                state = {"name": file.name, "total": 0, "done": 0, "started": started,
                         "phase": "parsing", "queued": 0, "running": 0}
                active[index] = state
                token = document_progress.set(state)
                metrics_token = _document_id.set(str(file))
                try:
                    tree = await dom_summary.summarize_markdown_object_tree_async(
                        file, client, model, lang=lang, show_progress=False,
                        leaf_min_len=leaf_min_len, min_len=min_len)
                    if not tree.get("blocks"):
                        raise ValueError("Empty Markdown document: no blocks to summarize")
                    if not isinstance(tree.get("summary"), str) or not tree["summary"].strip():
                        raise ValueError("Summarization returned no document summary")
                    dom_summary._write_semantics_tree(output, tree)
                    row["status"] = "completed"
                except asyncio.CancelledError:
                    progress_factory.write(f"Cancelled while processing: {file}")
                    raise
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    progress_factory.write(f"FAILED {file}: {row['error']}")
                finally:
                    _document_id.reset(metrics_token)
                    document_progress.reset(token)
                    active.pop(index)
                    totals["objects"] -= state["total"]
                    totals["done_objects"] -= state["done"]
                row.update(elapsed_seconds=round(perf_counter() - started, 3),
                           object_count=state["total"], completed_objects=state["done"])
                durations.append(row["elapsed_seconds"])
                if isinstance(client, SummaryAsyncClient):
                    row.update(client.metrics.snapshot(document=str(file)))
                if checkpoint:
                    checkpoint.record(file, row)
                results[index] = row
                totals["documents"] += 1
                totals["status"] = row["status"]
                if row["status"] == "completed":
                    progress_factory.write(f"COMPLETED {file} ({row['elapsed_seconds']:.1f}s)")

            async def worker():
                for index, file in pending:
                    await process_document(index, file)

            # Scope the instrumentation to this batch, just as the natural-text
            # runner does. Context variables keep concurrent documents separate.
            with patch.object(dom_summary, "extract_markdown_object_tree", extract_with_progress), \
                 patch.object(dom_summary, "_summarize_markdown_object_async", summarize_with_progress), \
                 patch.object(dom_summary, "_get_summary_semaphore", semaphore_with_progress):
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
        if image_token is not None:
            dom_summary._ignored_images.reset(image_token)
        _batch_active = False
        if checkpoint:
            checkpoint.save()
    return results
