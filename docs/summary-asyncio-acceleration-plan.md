# Accelerate cell 7’s Ollama summarization

## Summary

Keep the existing prompts, summarization stages, and `s` behavior. Improve how
independent requests overlap, reduce notebook overhead, and tune Ollama’s server
parallelism.

Cell 7 already uses four document workers and a shared eight-request semaphore.
The confirmed scheduling issue is that single-child traversal consumes task slots:
a mock test improved from **0.323s to 0.041s** by avoiding those reservations, with
identical request counts. Actual Ollama gains need benchmarking.

## Implementation changes

- In `nbs/01.core.dom.summary.natural_text.ipynb`, make `_ordered_summary_map` execute
  single-item inputs inline without reserving a worker slot. Retain ordered
  results, bounded tasks, cancellation cleanup, and the shared text/image request
  semaphore. Regenerate the Python module.
- In `nbs/05.application.summary.ipynb`, configure document, request, and traversal
  limits before importing DOM modules. Start with **4 document workers, 8 request
  slots, and 32 traversal slots**, respecting explicit environment overrides.
  Display effective settings and explain that changing import-time settings
  requires a kernel restart.
- Move each document’s synchronous `dom.setup()` into `asyncio.to_thread` so file
  loading and Pandoc conversion do not block ongoing Ollama requests. Keep DOM
  construction and summarization on the event loop; await outstanding setup work
  during cancellation.
- Update progress counters immediately, but render bars and active-document text
  through one background task every **250ms**. Flush on completion and await
  renderer cleanup on cancellation.
- Add notebook-level instrumentation around `AsyncClient.chat`: request count,
  peak concurrent calls, latency, and Ollama’s returned load/prompt/generation
  timing. Keep the shared client and existing document failure isolation.

No public summarization API or output schema changes are required. Application
helpers may be extracted into `utils/summary_asyncio.py` for offline testing and
reuse by the benchmark.

## Ollama server tuning

- Verify the remote server version, GPU residency, and configured parallelism.
  The configured endpoint was unreachable during planning, so its capacity is
  unknown.
- Benchmark `OLLAMA_NUM_PARALLEL=1`, then **2**, then **4** if memory permits,
  restarting the server between settings. Parallel inference increases
  context-memory requirements. See the [Ollama concurrency documentation](https://docs.ollama.com/faq#how-does-ollama-handle-concurrent-requests).
- For each server setting, compare client request limits equal to the server
  parallelism and twice that value, capped at 8. Keep the model and generation
  settings unchanged.
- Select the configuration with the highest median throughput; prefer lower
  concurrency when results differ by less than 5%. Increasing queue size is
  excluded because it does not increase inference capacity.

## Validation and acceptance

- Add a deeply nested single-child fixture with independent leaves: verify
  concurrent provider calls reach the request limit without exceeding the task
  limit.
- Compare deterministic mocked outputs and request counts against the current
  pipeline; preserve node order, empty `s` fields, parent dependencies, and image
  handling.
- Run existing concurrency, failure, cancellation, and summarization tests. Add
  notebook checks for throttled progress and setup cleanup.
- Benchmark the same representative text, image, and nested AST samples before
  and after, using temporary output destinations and three warmed runs. Report
  wall time, requests/second, peak concurrency, and errors.
- Require improved mock concurrency and no correctness regressions. Report
  measured server gains without promising a fixed speedup.

## Assumptions

Repeated summarization calls remain intact. Server tuning is applied separately
on the remote host. Preserve unrelated notebook edits and existing corpus
artifacts during benchmarking.

## Implementation and validation — 2026-10-12

- Implemented the inline-singleton scheduler in the source notebook and exported
  module. Existing prompt content, thresholds, request stages, ordering, and empty
  `s` behavior remain intact.
- Cell 7 now calls the tested runner in `utils/summary_asyncio.py`. Defaults are
  4 document workers, 8 shared request slots, and 32 traversal tasks per document.
  Setup runs in background threads; cancellation waits for their completion.
  Progress renders every 250ms, and request metrics appear in the results cell.
- The benchmark is `utils/benchmark_summary_asyncio.py`. It saves a fixed sample
  manifest, summary outputs, and `report.json` in a temporary directory. Live
  samples include text above the summarization threshold, accessible images, and
  nested containers with independent image leaves. Warmups are excluded from
  medians; timeouts stop the comparison and retain diagnostics.

### Offline results

Three measured runs after warmup, with 20ms simulated provider latency:

| Scheduler | Task limit | Median wall time | Requests per run | Median requests/s |
| --- | ---: | ---: | ---: | ---: |
| Original | 8 | 0.387s | 48 | 124.1 |
| Inline singletons | 32 | 0.124s | 48 | 385.7 |

This is approximately **3.1× faster in the mock benchmark**. Both variants had
zero errors and the same request count. This is not a prediction of live Ollama
performance. Regression tests also compare deterministic outputs and request
multisets against the frozen original scheduler.

Reproduce with:

```sh
.venv/bin/python utils/benchmark_summary_asyncio.py
.venv/bin/python -m pytest tests/test_summary_concurrency.py tests/test_summary_subnodes.py tests/test_summary_asyncio.py tests/test_textualize_lifecycle.py tests/test_dom_behavior.py tests/test_reorg.py -q
```

Threaded tests must run in an environment supporting normal thread-to-event-loop
notifications. The execution sandbox stalled during default-executor shutdown;
the normal runtime passed 79 summarization/DOM regression tests. An additional
benchmark-timeout regression verifies cancellation and saved diagnostics.

### Remote server findings

SSH inspection of `x@172.27.74.16` found Ollama 0.32.14, an active systemd service,
and **`OLLAMA_NUM_PARALLEL=4` already configured**. The RTX 4090 has 24,564 MiB of
VRAM; other inference processes were using approximately 16,503 MiB. The
configured Q4_K_M 27.3B vision model occupies 17GB on disk.

An empty-chat preload followed by `/api/ps` reported a loaded allocation of
18,687,323,009 bytes, with only 4,614,132,857 bytes on the GPU and a context length
of 32,768. This confirms substantial CPU offloading and makes available GPU
memory a material constraint on throughput.

A live comparison using the initial cached sample selection, 4 request slots,
and a 120-second pass limit timed out during its first warmup. No live speedup is
claimed. The benchmark retains timeout diagnostics and cancels client tasks.
Server settings were not changed: noninteractive sudo requires a password, and
unrelated inference processes were left running.

Before comparing server parallelism settings 1, 2, and 4, arrange sufficient GPU
capacity for this model and administrative access to restart the service. Then
rerun the fixed-sample benchmark with unchanged prompts and generation options:

```sh
.venv/bin/python utils/benchmark_summary_asyncio.py --live --request-limit 4 --server-parallel 4
.venv/bin/python utils/benchmark_summary_asyncio.py --live --request-limit 8 --server-parallel 4
```

Restart the notebook kernel and run its cells in order to pick up the new module
and import-time concurrency settings. The corpus was not rerun or overwritten
during implementation.
