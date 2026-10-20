"""Batch object-tree summarization for the operational notebook.

Importing this module does not import DOM modules or contact a model provider.
"""

import asyncio
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
import json
import math
import os
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from tqdm.auto import tqdm

from utils.summary_asyncio import SummaryAsyncClient, SummaryCheckpoint, _document_id


_batch_active = False



# Badges remain visible in both notebook widgets and terminal progress output.
_SERVER_STYLES = (
    ("🔵", "#2563eb"), ("🟠", "#ea580c"), ("🟢", "#16a34a"),
    ("🟣", "#9333ea"), ("🔴", "#dc2626"), ("🟡", "#ca8a04"),
)


def _server_progress(client, document=None):
    """Describe actual server activity; a document can use several servers."""
    labels, busiest, colour = [], 0, None
    for index, (host, metrics) in enumerate(zip(
            getattr(client, 'hosts', ()), getattr(client, 'server_metrics', ()))):
        badge, server_colour = _SERVER_STYLES[index % len(_SERVER_STYLES)]
        count = metrics.active if document is None else metrics.document_active.get(document, 0)
        if document is None or count:
            labels.append(f"{badge} S{index + 1} {host}: {count}")
        if count > busiest:
            busiest, colour = count, server_colour
    return " | ".join(labels), colour


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


def _prepare_incremental_tree(file, tree, dom_summary):
    """Reconcile current object children with saved records without rebuilding the DOM.

    Identity includes source content so a summary is never moved to a different
    item. Record and wrapper summaries both supply reusable text; annotations,
    original Pandoc subtrees, and existing ancestor summaries are retained.
    """
    if not isinstance(tree, dict) or not isinstance(tree.get("blocks"), list):
        raise ValueError(f"Invalid saved object tree: {file}")
    has_summary = dom_summary.object_utils._has_object_summary

    def key(record):
        return record["type"], str(record["id"]), record["content"]

    expected = {key(record): record for record in dom_summary.extract_markdown_objects(file)}
    roots = list(dom_summary._object_nodes(tree))
    if len(roots) != len(expected) or {key(node["object"]) for node in roots} != set(expected):
        raise ValueError(f"Saved object locations do not match current Markdown: {file}")
    work, missing, changed = [], 0, False
    for root in roots:
        records, wrappers = {}, {}

        def remember_record(record):
            identity = key(record)
            saved = records.setdefault(identity, dict(record))
            if not has_summary(saved) and has_summary(record):
                saved.update(summary=record["summary"])
            for child in record.get("children", []):
                remember_record(child)

        def remember_wrapper(node):
            record = node["object"]
            remember_record(record)
            identity = key(record)
            wrappers.setdefault(identity, node)
            if not has_summary(records[identity]) and isinstance(node.get("s"), str) and node["s"].strip():
                records[identity]["summary"] = node["s"]
            for child in node.get("subnodes", []):
                remember_wrapper(child)

        remember_wrapper(root)
        used = set()
        root_missing = 0

        def merge(record):
            nonlocal root_missing
            identity = key(record)
            used.add(identity)
            merged = {**record, **records.get(identity, {})}
            if "children" in record:
                merged["children"] = [merge(child) for child in record["children"]]
            if not has_summary(merged):
                root_missing += 1
            return merged

        def wrap(record):
            node = dict(wrappers.get(key(record), {"t": "Div", "c": [["", [], []], []]}))
            node.update(object=record, s=record.get("summary", ""))
            if "children" in record:
                node["subnodes"] = [wrap(child) for child in record["children"]]
            return node

        merged = merge(expected[key(root["object"])])
        if any(has_summary(record) for identity, record in records.items() if identity not in used):
            raise ValueError(f"Saved object descendants cannot be preserved for {file}: {root['object']['id']}")
        updated = wrap(merged)
        changed = changed or updated != root
        root.clear()
        root.update(updated)
        if root_missing:
            work.append(root)
            missing += root_missing
    return work, missing, changed


def _sync_object_summaries(node):
    """Refresh mirrored s fields after missing object summaries have been filled."""
    node["s"] = node["object"]["summary"]
    for child in node.get("subnodes", []):
        _sync_object_summaries(child)


def _complete_object(record, dom_summary):
    return (dom_summary.object_utils._has_object_summary(record)
            and all(_complete_object(child, dom_summary) for child in record.get('children', [])))


def _partial_path(file):
    return file.with_name(f"{file.stem}_summary-progress.json")


def _save_partial(file, checkpoint, tree):
    """Keep unpublished work separate from the completed semantics/checkpoint."""
    path = _partial_path(file)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(dict(config=checkpoint.config, signature=checkpoint.signature(file),
                           tree=tree), stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_partial(file, checkpoint):
    path = _partial_path(file)
    if not path.exists():
        return None
    saved = json.loads(path.read_text(encoding='utf-8'))
    def compatible(config):
        config = dict(config)
        config.pop('host', None)
        config['settings'] = {k: v for k, v in config.get('settings', {}).items()
                              if k != 'object_schema_version'}
        return config
    if (compatible(saved['config']) == compatible(checkpoint.config)
            and saved['signature'] == checkpoint.signature(file)):
        return saved['tree']
    return None


async def summarize_object_documents(
    files, client, model, *, corpus="", concurrency=4, progress_interval=0.25,
    checkpoint_path=None, resume=True, checkpoint_host=None, lang="zh",
    leaf_min_len=100, min_len=None, progress_factory=tqdm, completed_callback=None,
):
    """Summarize Markdown directly into atomic, recursively structured outputs.

    Results follow input order. A document is published only after its full tree
    succeeds. Failures are reported independently; cancellation awaits outstanding
    requests and saves completed-document checkpoints. Unpublished per-file progress
    retains completed objects and descendants on interruption. AST caches are not written.
    Reuse completed documents across additive summarizer features and schema
    versions. Completed outputs are checked for missing object descendants and
    summaries; fill only those gaps while retaining existing summaries, including
    parent/prose/document text. Publish upgrades atomically and checkpoint them;
    failures retain the previous completed output and checkpoint for retry.
    Set resume=False only when intentionally rebuilding every document.
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
            checkpoint_path, model, checkpoint_host, resume, ignore_host=True,
            ignore_settings=("object_schema_version",),
            settings={"summarizer": "object", "object_schema_version": 5, "lang": lang,
                      "leaf_min_len": leaf_min_len, "min_len": min_len,
                      "ignored_images_signature": snapshot.signature,
                      "generation": ({"think": client.think, "max_tokens": client.max_tokens}
                                     if isinstance(client, SummaryAsyncClient) else None)},
        ) if checkpoint_path is not None else None)
        saved_rows = [checkpoint.completed(file) if checkpoint else None for file in files]
        results = [None] * len(files)
        pending = iter(enumerate(files))
        durations = []
        resumed_count = 0
        request_offset = len(client.metrics.records) if isinstance(client, SummaryAsyncClient) else 0
        totals = {"objects": 0, "done_objects": 0,
                  "documents": 0, "status": "starting"}
        with progress_factory(total=len(files), desc="Documents", unit="doc", position=0,
                              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}{postfix}]") as documents, \
             progress_factory(total=0, desc="Objects (active documents)", unit="object", position=1,
                              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}{postfix}]") as objects, ExitStack() as file_displays:
            file_bars = [file_displays.enter_context(progress_factory(
                total=0, desc=f"File slot {slot + 1}", unit="object", position=slot + 2,
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} {postfix}", leave=False,
            )) for slot in range(min(concurrency, len(files)))]

            def render():
                documents.n = totals["documents"]
                status = dict(status=totals["status"], active=len(active), resumed=resumed_count)
                if durations:
                    status.update(last_doc=f"{durations[-1]:.1f}s", mean_doc=f"{sum(durations) / len(durations):.1f}s")
                if isinstance(client, SummaryAsyncClient):
                    status.update(requests=len(client.metrics.records) - request_offset,
                                  in_flight=len(client.active_requests))
                    if callable(getattr(client, "server_status", None)):
                        status["servers"] = _server_progress(client)[0] or client.server_status()
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
                for slot, bar in enumerate(file_bars):
                    state = next((state for state in active.values() if state['slot'] == slot), None)
                    bar.total = state['total'] if state else 0
                    bar.n = state['done'] if state else 0
                    status_text = (
                        f"{state['name']}: objects {state['done']}/{state['total']}, "
                        f"{state['phase']}, queued={state['queued']}, running={state['running']} "
                        f"({perf_counter() - state['started']:.0f}s)"
                        if state else 'Idle')
                    server_text, server_colour = _server_progress(
                        client, state['name']) if state else ('', None)
                    bar.colour = server_colour
                    if server_text:
                        status_text += f" | {server_text}"
                    bar.set_postfix_str(status_text, refresh=False)
                    bar.refresh()
                objects.refresh()
                documents.refresh()
                if completed_callback is not None:
                    completed = {
                        file: dict(entry['row'], file=file)
                        for file, entry in (checkpoint.entries.items() if checkpoint else ())
                        if entry.get('row', {}).get('status') == 'completed'}
                    for row in results:
                        if row is not None and row['status'] == 'completed':
                            completed[row['file']] = row
                    completed_callback(list(completed.values()))

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
                state = document_progress.get()
                tree = state.get('partial') if state is not None else None
                if tree is None:
                    tree = extract_tree(file)
                if state is not None:
                    state["total"] = sum(1 for _ in dom_summary._object_nodes(tree))
                    totals["objects"] += state["total"]
                    state['tree'] = tree
                    roots = list(dom_summary._object_nodes(tree))
                    state['reused_ids'] = {
                        str(node['object']['id']) for node in roots
                        if _complete_object(node['object'], dom_summary)}
                    state['done'] = len(state['reused_ids'])
                    totals['done_objects'] += state['done']
                    state["phase"] = "summarizing tree"
                return tree

            async def summarize_with_progress(*args, **kwargs):
                state = document_progress.get()
                if state is not None and checkpoint is not None:
                    kwargs['reuse_existing'] = True
                result = await summarize_object(*args, **kwargs)
                if state is not None:
                    obj = args[0] if args else kwargs['obj']
                    if str(obj['id']) not in state.get('reused_ids', set()):
                        state["done"] += 1
                        totals["done_objects"] += 1
                    if checkpoint is not None and state.get('tree') is not None:
                        _save_partial(Path(state['name']), checkpoint, state['tree'])
                    if state["done"] == state["total"]:
                        state["phase"] = "objects done; prose/parents pending"
                return result

            async def process_document(index, file, slot):
                nonlocal resumed_count
                started = perf_counter()
                output = file.with_name(f"{file.stem}_semantics.json")
                row = {"corpus": str(corpus), "file": str(file), "status": "failed",
                       "output": str(output), "error": "", "resumed": False,
                       "incremental": False, "filled_summaries": 0}
                state = {"slot": slot, "name": str(file), "total": 0, "done": 0, "started": started,
                         "phase": "parsing", "queued": 0, "running": 0}
                active[index] = state
                token = document_progress.set(state)
                metrics_token = _document_id.set(str(file))
                try:
                    if checkpoint is not None and resume and saved_rows[index] is None:
                        state['partial'] = _load_partial(file, checkpoint)
                        if state['partial'] is not None:
                            _prepare_incremental_tree(file, state['partial'], dom_summary)
                            row['resumed'] = True
                    if saved_rows[index] is not None:
                        state["phase"] = "checking saved summaries"
                        tree = json.loads(output.read_text(encoding="utf-8"))
                        work, missing, changed = _prepare_incremental_tree(file, tree, dom_summary)
                        if not work and not changed:
                            results[index] = dict(saved_rows[index], corpus=str(corpus),
                                                  incremental=False, filled_summaries=0)
                            resumed_count += 1
                            totals["documents"] += 1
                            totals["status"] = "resumed"
                            return
                        row["incremental"] = True
                        state["total"] = len(work)
                        totals["objects"] += state["total"]
                        state["phase"] = "filling missing summaries"

                        async def fill_object(position, node):
                            await summarize_with_progress(
                                node["object"], client, model, lang,
                                require_summary=True, reuse_existing=True)
                            _sync_object_summaries(node)

                        await dom_summary._ordered_summary_map(fill_object, work)
                        state["phase"] = "missing summaries filled; publishing"
                    else:
                        tree = await dom_summary.summarize_markdown_object_tree_async(
                            file, client, model, lang=lang, show_progress=False,
                            leaf_min_len=leaf_min_len, min_len=min_len)
                    if not tree.get("blocks"):
                        raise ValueError("Empty Markdown document: no blocks to summarize")
                    if not isinstance(tree.get("summary"), str) or not tree["summary"].strip():
                        raise ValueError("Summarization returned no document summary")
                    dom_summary._write_semantics_tree(output, tree)
                    row["status"] = "completed"
                    if row["incremental"]:
                        row["filled_summaries"] = missing
                except asyncio.CancelledError:
                    progress_factory.write(f"Cancelled while processing: {file}")
                    raise
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    progress_factory.write(f"FAILED {file}: {row['error']}")
                finally:
                    if checkpoint is not None and state.get('tree') is not None and row['status'] != 'completed':
                        try:
                            _save_partial(file, checkpoint, state['tree'])
                        except Exception as exc:
                            progress_factory.write(f"Could not save partial progress for {file}: {exc}")
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
                # A failed upgrade must retain the reusable completed entry;
                # otherwise its next rerun would regenerate the whole document.
                if checkpoint and (saved_rows[index] is None or row["status"] == "completed"):
                    checkpoint.record(file, row)
                if checkpoint is not None and row['status'] == 'completed':
                    _partial_path(file).unlink(missing_ok=True)
                results[index] = row
                totals["documents"] += 1
                totals["status"] = row["status"]
                if row["status"] == "completed":
                    progress_factory.write(f"COMPLETED {file} ({row['elapsed_seconds']:.1f}s)")

            async def worker(slot):
                for index, file in pending:
                    await process_document(index, file, slot)

            # Scope the instrumentation to this batch, just as the natural-text
            # runner does. Context variables keep concurrent documents separate.
            with patch.object(dom_summary, "extract_markdown_object_tree", extract_with_progress), \
                 patch.object(dom_summary, "_summarize_markdown_object_async", summarize_with_progress), \
                 patch.object(dom_summary, "_get_summary_semaphore", semaphore_with_progress):
                tasks = []
                display_task = asyncio.create_task(renderer())
                try:
                    render()
                    tasks = [asyncio.create_task(worker(slot)) for slot in range(min(concurrency, len(files)))]
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
