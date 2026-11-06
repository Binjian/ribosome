"""Batch object-tree summarization for the operational notebook.

Importing this module does not import DOM modules or contact a model provider.
"""

import asyncio
from contextlib import ExitStack, asynccontextmanager
from contextvars import ContextVar
import json
import hashlib
from copy import deepcopy
from collections import defaultdict, deque
import math
import os
import tempfile
from pathlib import Path
from queue import SimpleQueue, Empty
from time import perf_counter

from tqdm.auto import tqdm

from utils.summary_asyncio import SummaryAsyncClient, SummaryCheckpoint, _document_id, _claim_summary_resources


_batch_active = False


async def _run_resume_work(function, *args, on_progress, interval):
    """Keep rendering while local resume work runs; await it on cancellation."""
    events = SimpleQueue()
    task = asyncio.create_task(asyncio.to_thread(function, *args, progress=lambda *event: events.put(event)))

    def drain():
        latest = None
        while True:
            try:
                latest = events.get_nowait()
            except Empty:
                break
        if latest is not None:
            on_progress(*latest)

    try:
        while True:
            done, _ = await asyncio.wait((task,), timeout=interval)
            drain()
            if done:
                return task.result()
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.wait((task,), timeout=interval)
                drain()
            except asyncio.CancelledError:
                continue
        if not task.cancelled():
            task.exception()
        raise



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


def _prepare_incremental_tree(file, tree, dom_summary, *, progress=None):
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

    if progress:
        progress('resume: parsing source objects', 0, 0, 'objects')
    roots = list(dom_summary._object_nodes(tree))
    extracted = dom_summary.extract_markdown_objects(file)
    def source_key(record):
        return record['type'], record.get('line_start'), record.get('line_end'), record['content']
    previous_by_source = {source_key(root['object']): root['object'] for root in roots}
    for record in extracted:
        previous = previous_by_source.get(source_key(record))
        if previous is None:
            continue
        identities = defaultdict(deque)
        def remember(value):
            identities[source_key(value)].append(value['id'])
            for child in value.get('children', []):
                remember(child)
        remember(previous)
        aliases = {}
        extracted_root_id = record['id']
        def align(value):
            old_id = value['id']
            if identities.get(source_key(value)):
                value['id'] = identities[source_key(value)].popleft()
            elif str(old_id).startswith(str(extracted_root_id) + '.'):
                value['id'] = str(previous['id']) + str(old_id)[len(str(extracted_root_id)):]
            aliases[str(old_id)] = value['id']
            for child in value.get('children', []):
                align(child)
        align(record)
        def links(value):
            if isinstance(value, dict):
                for field in ('parent_id', 'list_id', 'table_id'):
                    if str(value.get(field)) in aliases:
                        value[field] = aliases[str(value[field])]
                for child in value.values():
                    links(child)
            elif isinstance(value, list):
                for child in value:
                    links(child)
        links(record)
    expected = {key(record): record for record in extracted}
    if len(roots) != len(expected) or {key(node["object"]) for node in roots} != set(expected):
        _reconcile_discovery(file, tree, dom_summary)
        work = [node for node in dom_summary._object_nodes(tree)
                if not _complete_object(node['object'], dom_summary)]
        audit = dom_summary.audit_completeness(tree, {'objects'})
        return work, len(audit['missing']), True
    work, missing, changed = [], 0, False
    if progress:
        progress('resume: checking saved objects', 0, len(roots), 'objects')
    for position, root in enumerate(roots, 1):
        records, wrappers = {}, {}

        def remember_record(record):
            identity = key(record)
            saved = records.setdefault(identity, dict(record))
            if not has_summary(saved) and has_summary(record):
                saved.update({k: record[k] for k in ("summary", "summary_skipped", "summary_ignored") if k in record})
            for child in record.get("children", []):
                remember_record(child)

        def remember_wrapper(node):
            record = node["object"]
            remember_record(record)
            identity = key(record)
            wrappers.setdefault(identity, node)
            if not has_summary(records[identity]) and isinstance(node.get("s"), str) and node["s"].strip():
                records[identity]["summary"] = node["s"]
            if not has_summary(records[identity]) and node.get("summary_skipped") is True and node.get("s") == "":
                records[identity].update(summary="", summary_skipped=True)
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
                previous = records.get(identity)
                if (previous and has_summary(previous) and any(key(child) not in records for child in record['children']) and
                        [child['id'] for child in previous.get('children', [])] !=
                        [child['id'] for child in merged['children']]):
                    merged['summary_coverage'] = 'historical'
            if not has_summary(merged):
                root_missing += 1
            return merged

        def wrap(record):
            node = dict(wrappers.get(key(record), {"t": "Div", "c": [["", [], []], []]}))
            node.update(object=record, s=record.get("summary", ""))
            node.pop("summary_skipped", None)
            if record.get("summary_skipped"):
                node["summary_skipped"] = True
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
        if progress:
            progress('resume: checking saved objects', position, len(roots), 'objects')
    return work, missing, changed


def _reconcile_discovery(file, tree, dom_summary):
    """Adopt newly recognized source objects without ordinal summary reassignment.

    This is restricted to a signature-compatible source. Source-shaped semantic
    nodes are matched in occurrence order; every existing nonempty summary must
    have a destination. Ambiguous/conflicting records fail before publication.
    """
    fresh = dom_summary.extract_markdown_object_tree(file)
    summary_fields = ('s', 'summary_skipped', 'summary_ignored', 'summary_provenance')

    def shape(value):
        if isinstance(value, list):
            result = []
            for child in value:
                if isinstance(child, dict) and 'object' in child:
                    result.extend(shape(child.get('c', [None, []])[1]))
                else:
                    result.append(shape(child))
            return result
        if isinstance(value, dict):
            return {key: shape(value[key]) for key in ('t', 'c', 'subnodes') if key in value
                    and not (key == 'subnodes' and 'object' in value)}
        return value

    def fingerprint(node):
        return json.dumps(shape(node), sort_keys=True, ensure_ascii=False)

    def semantic_nodes(value, *, source=False):
        if isinstance(value, list):
            for child in value:
                yield from semantic_nodes(child, source=source)
        elif isinstance(value, dict):
            if 'object' in value:
                if source:
                    yield from semantic_nodes(value.get('c', [None, []])[1], source=source)
                return
            if 't' in value:
                yield value
            for field in ('blocks', 'c', 'subnodes'):
                yield from semantic_nodes(value.get(field, []), source=source)

    saved_nodes = defaultdict(deque)
    for node in semantic_nodes(tree):
        if dom_summary._has_node_summary(node):
            saved_nodes[fingerprint(node)].append(node)
    for node in semantic_nodes(fresh, source=True):
        matches = saved_nodes.get(fingerprint(node))
        if matches:
            previous = matches.popleft()
            for field in summary_fields:
                if field in previous:
                    node[field] = deepcopy(previous[field])
    unmatched = [node for nodes in saved_nodes.values() for node in nodes
                 if isinstance(node.get('s'), str) and node['s'].strip()]
    if unmatched:
        raise ValueError(f'Cannot unambiguously preserve {len(unmatched)} saved semantic summaries for {file}')

    def records(record):
        yield record
        for child in record.get('children', []):
            yield from records(child)

    def locator(record):
        return (record['type'], record.get('line_start'), record.get('line_end'), record['content'])

    saved = defaultdict(deque)
    for wrapper in dom_summary._object_nodes(tree):
        for record in records(wrapper['object']):
            saved[locator(record)].append(record)
    aliases = {}
    for root_position, wrapper in enumerate(dom_summary._object_nodes(fresh)):
        def merge(record, occurrence):
            children = [merge(child, (*occurrence, position))
                        for position, child in enumerate(record.get('children', []))]
            matches = saved.get(locator(record))
            old_id = record['id']
            if matches:
                previous = matches.popleft()
                merged = {**record, **deepcopy(previous)}
                aliases[str(old_id)] = previous['id']
            else:
                identity = hashlib.sha256(repr((locator(record), occurrence)).encode()).hexdigest()[:16]
                merged = dict(record, id=f'source.{record["type"]}.{identity}')
                aliases[str(old_id)] = merged['id']
            if 'children' in record:
                merged['children'] = children
            return merged
        wrapper['object'] = merge(wrapper['object'], (root_position,))
        record = wrapper['object']
        original = wrapper.get('c', [None, []])[1]
        if (not dom_summary.object_utils._has_object_summary(record) and len(original) == 1
                and isinstance(original[0], dict) and dom_summary._has_node_summary(original[0])):
            record['summary'] = original[0]['s']
            for field in ('summary_skipped', 'summary_ignored'):
                if field in original[0]:
                    record[field] = original[0][field]
            record['coverage'] = 'coverage_unknown'
        wrapper['s'] = record.get('summary', '')
        wrapper['subnodes'] = dom_summary._row_summary_nodes(record)
    if any(dom_summary.object_utils._has_object_summary(record)
           for values in saved.values() for record in values):
        raise ValueError(f'Cannot unambiguously preserve saved object occurrences for {file}')
    for wrapper in dom_summary._object_nodes(fresh):
        for record in records(wrapper['object']):
            for field in ('parent_id', 'table_id', 'list_id'):
                if str(record.get(field)) in aliases:
                    record[field] = aliases[str(record[field])]
    # Title, document summary and unknown annotations remain owned by the saved
    # document. Only the reconciled semantic structure is adopted.
    tree['blocks'] = fresh['blocks']


def _sync_object_summaries(node):
    """Refresh mirrored s fields after missing object summaries have been filled."""
    node["s"] = node["object"]["summary"]
    node.pop("summary_skipped", None)
    if node["object"].get("summary_skipped"):
        node["summary_skipped"] = True
    for child in node.get("subnodes", []):
        _sync_object_summaries(child)


def _section_count(tree):
    """Count semantic sections, excluding retained source ASTs inside objects."""
    def count(node):
        if isinstance(node, list):
            return sum(count(child) for child in node)
        if not isinstance(node, dict) or 'object' in node:
            return 0
        return int(node.get('t') == 'Section') + sum(
            count(node.get(key, [])) for key in ('blocks', 'c', 'subnodes'))
    return count(tree)


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


def _load_partial(file, checkpoint, *, progress=None):
    path = _partial_path(file)
    if not path.exists():
        return None
    size = path.stat().st_size
    chunks, loaded = [], 0
    if progress:
        progress('resume: reading partial progress', 0, size, 'bytes')
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            chunks.append(chunk)
            loaded += len(chunk)
            if progress:
                progress('resume: reading partial progress', loaded, size, 'bytes')
    if progress:
        progress('resume: decoding partial JSON', 0, 0, 'objects')
    saved = json.loads(b''.join(chunks))
    if progress:
        progress('resume: verifying signatures/settings', 0, 0, 'objects')
    def compatible(config):
        config = dict(config)
        config.pop('host', None)
        config['settings'] = {k: v for k, v in {'input_token_budget': 8192, **config.get('settings', {})}.items()
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
    input_token_budget=8192, partial_snapshot_interval=1.0, artifact_repository=None,
):
    """Summarize Markdown directly into atomic, recursively structured outputs.

    Results follow input order. A document is published only after its full tree
    succeeds. Failures are reported independently; cancellation awaits outstanding
    requests and saves completed-document checkpoints. Unpublished per-file progress
    retains completed objects, descendants, prose and sections on interruption.
    AST caches are not written.
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
    dom_summary._positive_setting(input_token_budget, 'input_token_budget')
    if not isinstance(partial_snapshot_interval, (int, float)) or partial_snapshot_interval < 0:
        raise ValueError('partial_snapshot_interval must be nonnegative')
    files = [Path(file).resolve() for file in files]
    if not files:
        return []
    if checkpoint_path is None and artifact_repository is not None:
        checkpoint_path = Path(artifact_repository.root) / 'summary-checkpoints.json'
    document_progress = ContextVar("object_document_progress", default=None)
    extract_tree = dom_summary.extract_markdown_object_tree
    get_semaphore = dom_summary._default_summary_semaphore
    record_progress = dom_summary._default_record_summary_progress
    active = {}
    checkpoint = None
    image_token = None
    runtime_token = None
    resources = _claim_summary_resources(files, checkpoint_path)
    resources.__enter__()
    try:
        snapshot = await dom_summary._prepare_ignored_images()
        image_token = dom_summary._ignored_images.set(snapshot)
        checkpoint = (SummaryCheckpoint(
            checkpoint_path, model, checkpoint_host, resume, ignore_host=True,
            ignore_settings=("object_schema_version",),
            settings_defaults={'input_token_budget': 8192}, artifact_repository=artifact_repository,
            settings={"summarizer": "object", "object_schema_version": 5, "lang": lang,
                      "leaf_min_len": leaf_min_len, "min_len": min_len,
                      "input_token_budget": input_token_budget,
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
                status = dict(status=totals["status"], active=len(active), resumed=resumed_count,
                              sections=f"{sum(state['sections_done'] for state in active.values())}/"
                                       f"{sum(state['sections_total'] for state in active.values())}")
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
                    f"{state['phase']}, sections {state['sections_done']}/{state['sections_total']}, queued={state['queued']}, running={state['running']} "
                    f"({perf_counter() - state['started']:.0f}s)" for state in active.values()),
                    refresh=False)
                for slot, bar in enumerate(file_bars):
                    state = next((state for state in active.values() if state['slot'] == slot), None)
                    resume_progress = state.get('resume_progress') if state else None
                    bar.total = resume_progress[1] if resume_progress else state['total'] if state else 0
                    bar.n = resume_progress[0] if resume_progress else state['done'] if state else 0
                    status_text = (
                        f"{state['name']}: objects {state['done']}/{state['total']}, "
                        f"{state['phase']}, sections {state['sections_done']}/{state['sections_total']}, queued={state['queued']}, running={state['running']} "
                        f"({perf_counter() - state['started']:.0f}s)"
                        if state else 'Idle')
                    if resume_progress:
                        done, total, unit = resume_progress
                        if unit == 'bytes':
                            status_text += f' | resume {done / 1048576:.1f}/{total / 1048576:.1f} MiB'
                        elif total:
                            status_text += f' | resume {done}/{total} {unit}'
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
                    state['sections_total'] = _section_count(tree)
                    roots = list(dom_summary._object_nodes(tree))
                    state['reused_ids'] = {
                        str(node['object']['id']) for node in roots
                        if _complete_object(node['object'], dom_summary)}
                    state['known_ids'] = {str(node['object']['id']) for node in roots}
                    state['done_ids'] = set(state['reused_ids'])
                    state['done'] = len(state['done_ids'])
                    totals['done_objects'] += state['done']
                    state["phase"] = "summarizing tree"
                return tree

            def record_with_progress(node_type, path, file_path, table_count, section_count):
                state = document_progress.get()
                if node_type == 'Section' and state is not None:
                    section_count['count'] += 1
                    if path not in state['section_paths']:
                        state['section_paths'].add(path)
                        state['sections_done'] += 1
                else:
                    record_progress(node_type, path, file_path, table_count, section_count)

            async def summarize_with_progress(implementation, *args, **kwargs):
                state = document_progress.get()
                reused = False
                if state is not None:
                    obj = args[0] if args else kwargs['obj']
                    identity = str(obj['id'])
                    if (state.get('incremental') and identity not in state['known_ids']
                            and _complete_object(obj, dom_summary)):
                        await asyncio.sleep(0)
                        return obj
                    # Nested cell/list/image calls are discovered during traversal.
                    # Reserve their work before awaiting, so n never exceeds total.
                    if identity not in state['known_ids']:
                        state['known_ids'].add(identity)
                        state['total'] += 1
                        totals['objects'] += 1
                    if checkpoint is not None:
                        kwargs['reuse_existing'] = True
                        # Reconciled partial trees already include current descendants.
                        # Skip a complete subtree, including its checkpoint rewrite.
                        reused = _complete_object(obj, dom_summary)
                if reused:
                    result = obj
                    state['reused_checked'] = state.get('reused_checked', 0) + 1
                    state['phase'] = f"reusing saved objects: {state['reused_checked']} checked"
                else:
                    result = await implementation(*args, **kwargs)
                if state is not None:
                    if identity not in state['done_ids']:
                        state['done_ids'].add(identity)
                        state["done"] += 1
                        totals["done_objects"] += 1
                    if not reused and checkpoint is not None and state.get('tree') is not None:
                        if perf_counter() - state.get('last_snapshot', 0) >= partial_snapshot_interval:
                            snapshot_start = perf_counter()
                            _save_partial(Path(state['name']), checkpoint, state['tree'])
                            state['last_snapshot'] = perf_counter()
                            state['snapshot_seconds'] = state.get('snapshot_seconds', 0) + perf_counter() - snapshot_start
                    if not reused and state["done"] == state["total"]:
                        state["phase"] = "objects done; prose/parents pending"
                    # Reused summaries may complete without any provider awaits.
                    # Let the renderer and cancellation run during long traversals.
                    await asyncio.sleep(0)
                return result

            async def process_document(index, file, slot):
                nonlocal resumed_count
                started = perf_counter()
                output = file.with_name(f"{file.stem}_semantics.json")
                row = {"corpus": str(corpus), "file": str(file), "status": "failed",
                       "output": str(output), "error": "", "resumed": False,
                       "incremental": False, "filled_summaries": 0}
                state = {"slot": slot, "name": str(file), "total": 0, "done": 0, "started": started,
                         "phase": "parsing", "queued": 0, "running": 0,
                         "known_ids": set(), "done_ids": set(),
                         "sections_total": 0, "sections_done": 0, "section_paths": set()}
                active[index] = state
                token = document_progress.set(state)
                metrics_token = _document_id.set(str(file))

                def resume_progress(phase, done, total, unit):
                    state['phase'] = phase
                    state['resume_progress'] = (done, total, unit)
                    render()

                try:
                    input_signature = SummaryCheckpoint.signature(file)
                    input_assets = SummaryCheckpoint.asset_fingerprints(file)
                    historical_assets = False
                    if (checkpoint is not None and resume and saved_rows[index] is None
                            and _partial_path(file).is_file()):
                        resume_progress('resume: locating partial progress', 0, 0, 'objects')
                        state['partial'] = await _run_resume_work(
                            _load_partial, file, checkpoint, on_progress=resume_progress,
                            interval=progress_interval)
                        if state['partial'] is not None:
                            await _run_resume_work(
                                _prepare_incremental_tree, file, state['partial'], dom_summary,
                                on_progress=resume_progress, interval=progress_interval)
                            row['resumed'] = True
                        state.pop('resume_progress', None)
                    if saved_rows[index] is not None:
                        state["phase"] = "checking saved summaries"
                        tree = json.loads(output.read_text(encoding="utf-8"))
                        historical_assets = (tree.get('_source_provenance', {}).get('asset_provenance') == 'historical_unknown'
                            or '_source_provenance' not in tree
                            or checkpoint.entries.get(str(file.resolve()), {}).get('asset_provenance') == 'historical_unknown')
                        work, missing, changed = _prepare_incremental_tree(file, tree, dom_summary)
                        changed = changed or '_source_provenance' not in tree
                        audit = dom_summary.audit_completeness(tree)
                        missing = len(audit['missing'])
                        state['sections_total'] = _section_count(tree)
                        if not work and not changed and audit['complete']:
                            state['sections_done'] = state['sections_total']
                            results[index] = dict(saved_rows[index], corpus=str(corpus),
                                                  incremental=False, filled_summaries=0)
                            resumed_count += 1
                            totals["documents"] += 1
                            totals["status"] = "resumed"
                            return
                        row["incremental"] = True
                        state['incremental'] = True
                        state['known_ids'] = {str(node['object']['id']) for node in work}
                        state["total"] = len(state['known_ids'])
                        totals["objects"] += state["total"]
                        state["phase"] = "filling missing summaries"

                        # The saved tree is the authority: traversing it with reuse
                        # enabled repairs prose/section/document gaps as well as
                        # object descendants without requesting saved summaries.
                        state['tree'] = tree
                        tree = await dom_summary.summarize_markdown_object_tree_async(
                            file, client, model, lang=lang, show_progress=False,
                            leaf_min_len=leaf_min_len, min_len=min_len,
                            reuse_existing=True, saved_tree=tree)
                        state["phase"] = "missing summaries filled; publishing"
                    else:
                        tree = await dom_summary.summarize_markdown_object_tree_async(
                            file, client, model, lang=lang, show_progress=False,
                            leaf_min_len=leaf_min_len, min_len=min_len,
                            reuse_existing=state.get('partial') is not None)
                    if not tree.get("blocks"):
                        raise ValueError("Empty Markdown document: no blocks to summarize")
                    summary = tree.get("summary")
                    if not (isinstance(summary, str) and (summary.strip() or (tree.get("summary_skipped") is True and summary == ""))):
                        raise ValueError("Summarization returned no document summary")
                    final_audit = dom_summary.audit_completeness(tree)
                    if not final_audit['complete']:
                        raise ValueError(f"Incomplete summary publication: {len(final_audit['missing'])} missing summaries")
                    if (SummaryCheckpoint.signature(file) != input_signature
                            or SummaryCheckpoint.asset_fingerprints(file) != input_assets):
                        raise ValueError('Source or artifacts changed during summarization; retry against a stable input revision')
                    tree['_source_provenance'] = dict(
                        source_sha256=input_signature[str(file.resolve())], source_uri=str(file.resolve()),
                        assets=input_assets, asset_provenance='historical_unknown' if historical_assets else 'observed')
                    if artifact_repository is not None:
                        from ribosome.core.document import source_revision, adapt_semantics
                        document = artifact_repository.adopt_document(str(file))
                        revision = source_revision(document.id, str(file), file.read_bytes(),
                                                   assets=SummaryCheckpoint.asset_fingerprints(file))
                        graph = adapt_semantics(tree, document, revision)
                        publication = artifact_repository.prepare(graph, completeness=final_audit)
                        row['canonical_generation'] = publication.generation
                        row['canonical_repository'] = str(Path(artifact_repository.root).resolve())
                    row["status"] = "completed"
                    if row["incremental"]:
                        row["filled_summaries"] = missing
                    row.update(elapsed_seconds=round(perf_counter() - started, 3),
                               object_count=state['total'], completed_objects=state['done'],
                               section_count=state['sections_total'], completed_sections=state['sections_done'])
                    if checkpoint is not None:
                        checkpoint.publish(file, tree, row, dom_summary._write_semantics_tree)
                    else:
                        dom_summary._write_semantics_tree(output, tree)
                except asyncio.CancelledError:
                    progress_factory.write(f"Cancelled while processing: {file}")
                    raise
                except Exception as exc:
                    row['status'] = 'failed'
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
                           object_count=state["total"], completed_objects=state["done"],
                           section_count=state["sections_total"], completed_sections=state["sections_done"],
                           snapshot_seconds=state.get('snapshot_seconds', 0))
                durations.append(row["elapsed_seconds"])
                if isinstance(client, SummaryAsyncClient):
                    row.update(client.metrics.snapshot(document=str(file)))
                # A failed upgrade must retain the reusable completed entry;
                # otherwise its next rerun would regenerate the whole document.
                if checkpoint and 'publication_generation' not in row and (saved_rows[index] is None or row["status"] == "completed"):
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

            # Explicit context-local services allow independent batches to run
            # concurrently without patching functions or global batch guards.
            runtime_token = dom_summary._summary_runtime.set(dom_summary.SummaryRuntime(
                tree_factory=extract_with_progress, object_wrapper=summarize_with_progress,
                semaphore_factory=semaphore_with_progress, progress_callback=record_with_progress,
                input_token_budget=input_token_budget))
            if runtime_token is not None:
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
        if runtime_token is not None:
            dom_summary._summary_runtime.reset(runtime_token)
        try:
            if checkpoint:
                try:
                    checkpoint.save()
                except OSError as exc:
                    if any(row and row.get('publication_pending') for row in results):
                        progress_factory.write(f'Committed summaries need checkpoint projection retry: {exc}')
                    else:
                        raise
        finally:
            resources.__exit__(None, None, None)
    return results
