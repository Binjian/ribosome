"""Compare original and optimized summary scheduling without writing corpus files."""

import argparse
import asyncio
from contextlib import nullcontext, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from statistics import median
import sys
import tempfile
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch
from weakref import WeakKeyDictionary


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import dotenv_values
from ollama import AsyncClient
from utils.summary_asyncio import SummaryAsyncClient, _document_id, count_summary_nodes


async def _legacy_ordered_summary_map(fn, values):
    """Frozen pre-acceleration scheduler, including single-item slot reservations."""
    from ribosome.core.dom import summary as sut
    budget = sut._summary_budget.get()
    token = None
    if budget is None:
        budget = {"limit": sut._SUMMARY_TASK_LIMIT, "active": 0}
        token = sut._summary_budget.set(budget)
    tasks = []
    try:
        results = [None] * len(values)
        count = min(len(values), budget["limit"] - budget["active"])
        if count == 0:
            for index, value in enumerate(values):
                results[index] = await fn(index, value)
            return results
        pending = iter(enumerate(values))

        async def worker():
            try:
                for index, value in pending:
                    results[index] = await fn(index, value)
            finally:
                budget["active"] -= 1

        for _ in range(count):
            budget["active"] += 1
            tasks.append(asyncio.create_task(worker()))
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return results
    finally:
        if token is not None:
            sut._summary_budget.reset(token)


def synthetic_samples():
    leaves = [{"t": "Str", "c": f"{index}: " + "x" * 400} for index in range(16)]
    nested = {"t": "Section", "c": deepcopy(leaves)}
    for _ in range(10):
        nested = {"t": "Section", "c": [nested]}
    return [
        {"name": "text", "root": PROJECT_ROOT, "blocks": leaves},
        {"name": "images", "root": PROJECT_ROOT, "blocks": [
            {"t": "Image", "c": [[], [], [f"synthetic-{i}.png", ""]]} for i in range(16)]},
        {"name": "nested", "root": PROJECT_ROOT, "blocks": [nested]},
    ]


def cached_samples(input_dir, max_nodes=300, max_images=8):
    """Select stable text/image subtrees and deep nesting from cached ASTs only."""
    text = []
    images = []
    deepest = None

    def text_size(value):
        if isinstance(value, dict):
            if value.get("t") in {"Image", "Cite", "AlignDefault", "ColWidth"}:
                return 0
            return sum(text_size(v) for k, v in value.items() if k not in {"t", "s"})
        if isinstance(value, list):
            return sum(text_size(v) for v in value)
        return len(value.strip()) if isinstance(value, str) else 0

    def image_count(value):
        if isinstance(value, dict):
            if value.get("t") in {"Cite", "AlignDefault", "ColWidth"}:
                return 0
            if value.get("t") == "Image":
                return 1
            return sum(image_count(v) for k, v in value.items() if k not in {"t", "s"})
        if isinstance(value, list):
            return sum(image_count(v) for v in value)
        return 0

    def visit(value, root, source, depth=0):
        nonlocal deepest
        if isinstance(value, dict):
            if value.get("t") in {"Cite", "AlignDefault", "ColWidth"}:
                return
            if value.get("t") == "Image":
                try:
                    image = Path(value["c"][2][0])
                    if not image.is_absolute():
                        image = root / image
                    if image.is_file() and len(images) < max_images:
                        images.append({"name": f"image-{len(images)}", "root": root,
                                       "source": source, "blocks": [deepcopy(value)]})
                except (KeyError, IndexError, TypeError):
                    pass
                return
            count = count_summary_nodes(value)
            if value.get("t") in {"Para", "Plain", "Table", "Section"} and value.get("c") \
                    and 1 <= count <= max_nodes and not image_count(value) and text_size(value) >= 300:
                sample = {"name": f"text-{len(text)}", "root": root, "source": source,
                          "blocks": [deepcopy(value)]}
                if len(text) < 4:
                    text.append(sample)
                if deepest is None or depth > deepest[0]:
                    deepest = (depth, sample)
            for key, child in value.items():
                if key not in {"t", "s"}:
                    visit(child, root, source, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, root, source, depth + 1)

    for source in sorted(Path(input_dir).rglob("*_ast.json")):
        visit(json.loads(source.read_text(encoding="utf-8")), source.parent, str(source))
    if not text or not images:
        raise ValueError("Live benchmark requires cached text ASTs and accessible image files")
    samples = list(text)
    if images:
        # Group images into a single sample so within-document concurrency is exercised.
        for image in images:
            node = image["blocks"][0]
            link = Path(node["c"][2][0])
            node["c"][2][0] = str((image["root"] / link).resolve())
        samples.append({"name": "images", "root": PROJECT_ROOT,
                        "source": [image["source"] for image in images],
                        "blocks": [image["blocks"][0] for image in images]})
    if deepest is not None:
        # Singleton wrappers reproduce the deep traversal case around actual content.
        sample = deepcopy(deepest[1])
        sample["name"] = "nested"
        # Include independent image leaves so this case exercises concurrency even
        # when all text requests are parent aggregates rather than long Str leaves.
        image_leaves = [deepcopy(image["blocks"][0]) for image in images[:4]]
        node = {"t": "Section", "c": sample["blocks"] + image_leaves}
        for _ in range(10):
            node = {"t": "Section", "c": [node]}
        sample["blocks"] = [node]
        samples.append(sample)
    return samples


async def run_variant(samples, client, output_dir, variant, *, task_limit,
                      request_limit, document_limit, scheduler=None):
    from ribosome.core.dom import summary as sut
    client.reset_metrics()
    results = [None] * len(samples)
    pending = iter(enumerate(samples))

    async def worker():
        for index, sample in pending:
            token = _document_id.set(sample["name"])
            started = perf_counter()
            row = {"sample": sample["name"], "status": "completed", "error": ""}
            try:
                blocks = await sut.summarize_node_async(
                    deepcopy(sample["blocks"]), Path(sample["root"]), client, client.summary_model,
                    Path(sample["name"]), {"count": 0}, {"count": 0},
                    leaf_min_len=100, min_len=300,
                )
                final = await sut._summarize_leaf_async(
                    " ".join([sample["name"], *[b.get("s", "") for b in blocks]]),
                    100, 300, client, client.summary_model, "zh")
                (output_dir / f"{variant}-{index}.json").write_text(
                    json.dumps({"blocks": blocks, "summary": final}, ensure_ascii=False), encoding="utf-8")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                _document_id.reset(token)
            row["wall_seconds"] = perf_counter() - started
            row.update(client.metrics.snapshot(document=sample["name"]))
            results[index] = row

    start = perf_counter()
    with patch.object(sut, "_SUMMARY_TASK_LIMIT", task_limit), \
         patch.object(sut, "_SUMMARY_CONCURRENCY", request_limit), \
         patch.object(sut, "_summary_semaphores", WeakKeyDictionary()), \
         patch.object(sut, "_ordered_summary_map", scheduler or sut._ordered_summary_map), \
         redirect_stdout(io.StringIO()):
        tasks = [asyncio.create_task(worker()) for _ in range(min(document_limit, len(samples)))]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = perf_counter() - start
    return {"variant": variant, "task_limit": task_limit, "request_limit": request_limit,
            "document_limit": document_limit, "wall_seconds": elapsed,
            "failed_samples": sum(row["status"] == "failed" for row in results),
            **client.metrics.snapshot(wall_seconds=elapsed), "samples": results}


async def benchmark(args):
    config = dotenv_values(PROJECT_ROOT / ".env")
    host = args.host or os.getenv("OLLAMA_HOST") or config.get("OLLAMA_HOST")
    model = args.model or os.getenv("OLLAMA_SUMMARY_MODEL") or config.get("OLLAMA_SUMMARY_MODEL")
    if args.live and (not host or not model):
        raise ValueError("Live mode requires OLLAMA_HOST and OLLAMA_SUMMARY_MODEL or --host/--model")
    samples = cached_samples(args.input_dir) if args.live else synthetic_samples()
    output_dir = Path(args.output_dir) if args.output_dir else Path(tempfile.mkdtemp(prefix="summary-asyncio-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "samples.json").write_text(
        json.dumps(samples, default=str, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    client = SummaryAsyncClient(host=host if args.live else "http://localhost:11434")
    client.summary_model = model if args.live else "offline"
    async def simulated_chat(self, *positional, **kwargs):
        await asyncio.sleep(args.mock_latency)
        return SimpleNamespace(message=SimpleNamespace(content="short"),
                               eval_duration=int(args.mock_latency * 1e9), eval_count=1)

    variants = [("baseline", args.baseline_task_limit, _legacy_ordered_summary_map),
                ("optimized", args.task_limit, None)]
    runs = []
    warmups = []

    async def run(variant, limit, scheduler):
        started = perf_counter()
        try:
            row = await asyncio.wait_for(
                run_variant(samples, client, output_dir, variant, task_limit=limit,
                            request_limit=args.request_limit, document_limit=args.document_limit,
                            scheduler=scheduler), args.timeout_seconds)
        except TimeoutError:
            elapsed = perf_counter() - started
            row = {"variant": variant, "task_limit": limit, "wall_seconds": elapsed,
                   "failed_samples": len(samples), "samples": [],
                   "error": f"Timed out after {args.timeout_seconds}s; model requests cancelled",
                   **client.metrics.snapshot(wall_seconds=elapsed)}
        print(f"{variant}: {row['wall_seconds']:.3f}s, {row['request_count']} requests, "
              f"{row['failed_samples']} failed samples", flush=True)
        return row

    try:
        if args.live:
            response = await client.list()
            if client.summary_model not in {model.model for model in response.models}:
                raise ValueError(f"Model {client.summary_model!r} is unavailable on {host}")
        with patch.object(AsyncClient, "chat", simulated_chat) if not args.live else nullcontext():
            for variant, limit, scheduler in variants:
                # Exclude one warmup pass per variant from measured medians.
                warmups.append(await run(f"{variant}-warmup", limit, scheduler))
                if warmups[-1]["failed_samples"]:
                    break
            if not any(row["failed_samples"] for row in warmups):
                for repeat in range(args.repeats):
                    for variant, limit, scheduler in variants:
                        row = await run(f"{variant}-{repeat}", limit, scheduler)
                        row.update(variant=variant, repeat=repeat)
                        runs.append(row)
                    if any(row["failed_samples"] for row in runs):
                        break
    finally:
        # ollama 0.6.2 has no public close() method.
        await client._client.aclose()
    summary = []
    for variant, limit, _ in variants:
        rows = [row for row in runs if row["variant"] == variant]
        if not rows:
            continue
        summary.append({"variant": variant, "task_limit": limit,
                        "median_wall_seconds": median(row["wall_seconds"] for row in rows),
                        "median_requests_per_second": median(row["requests_per_second"] for row in rows),
                        "request_counts": [row["request_count"] for row in rows],
                        "peak_requests": max(row["peak_requests"] for row in rows),
                        "failed_samples": sum(row["failed_samples"] for row in rows),
                        "request_errors": sum(row["request_errors"] for row in rows)})
    report = {"mode": "live" if args.live else "mock", "host": host if args.live else None,
              "model": client.summary_model, "server_parallel": args.server_parallel,
              "request_limit": args.request_limit, "document_limit": args.document_limit,
              "samples": [{"name": sample["name"], "source": sample.get("source"),
                           "nodes": count_summary_nodes(sample["blocks"])} for sample in samples],
              "summary": summary, "warmups": warmups, "runs": runs}
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Report and temporary summaries: {report_path}")
    if any(row["failed_samples"] or row["request_errors"] for row in summary + warmups):
        raise RuntimeError("Benchmark had failures; inspect report before comparing throughput")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the configured Ollama server")
    parser.add_argument("--host")
    parser.add_argument("--model")
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "assets/md.json.semantics.summary")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--request-limit", type=int, default=8)
    parser.add_argument("--task-limit", type=int, default=32)
    parser.add_argument("--baseline-task-limit", type=int, default=8)
    parser.add_argument("--document-limit", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--server-parallel", type=int, help="Record server setting; does not change it")
    parser.add_argument("--mock-latency", type=float, default=0.02)
    parser.add_argument("--timeout-seconds", type=float, default=300,
                        help="Maximum seconds per pass; cancel and report slow/failing runs")
    args = parser.parse_args()
    for field in ("request_limit", "task_limit", "baseline_task_limit", "document_limit", "repeats"):
        if getattr(args, field) < 1:
            parser.error(f"--{field.replace('_', '-')} must be positive")
    import math
    if not math.isfinite(args.mock_latency) or args.mock_latency < 0:
        parser.error("--mock-latency must be finite and nonnegative")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be finite and positive")
    if args.server_parallel is not None and args.server_parallel < 1:
        parser.error("--server-parallel must be positive")
    asyncio.run(benchmark(args))


if __name__ == "__main__":
    main()
