# Summary utilities

[Utility index](../README.md) · [Object-summary notebook](../../nbs/05.application.summary.object.ipynb) · [Natural-text notebook](../../nbs/05.application.summary.natural_text.ipynb)

These helpers run bounded asynchronous summary batches, record provider activity,
and resume completed work. The notebooks provide corpus selection, configuration,
preflight, progress displays, and result inspection. Importing the helper modules
does not start a summary batch.

All shell commands below run from the repository root using the project's
environment. Prepare generated DOM modules before using the runners:

```sh
uv run ribosome-prepare --profile standalone
```

Preparation exports notebook code; it does not execute the notebooks or call a
model. See [preparation profiles](../../docs/preparation-profiles.md) for setup.
Live summaries also need reachable Ollama servers and the configured text/image
model installed on every selected summary server.

## Module guide

| Module | Use it for |
| --- | --- |
| [summary_asyncio.py](summary_asyncio.py) | `configure_summary_limits`, non-streaming `SummaryAsyncClient`, `RequestMetrics`, atomic `SummaryCheckpoint`, and the natural-text `summarize_documents` runner. |
| [summary_object_asyncio.py](summary_object_asyncio.py) | `discover_markdown` and `summarize_object_documents`: recursive object trees, per-document progress, incremental gap filling, and atomic publication. |
| [summary_ollama_pool.py](summary_ollama_pool.py) | `OllamaSummaryPool`: route requests across server slots, retry timeouts, preflight models, and report per-server metrics. |
| [benchmark_summary_asyncio.py](benchmark_summary_asyncio.py) | Compare the original and current natural-text scheduling with synthetic requests or selected cached AST samples. |

The runner functions are Python APIs, not command-line programs. The benchmark
has a CLI:

```sh
.venv/bin/python -m utils.summary.benchmark_summary_asyncio --help
```

## Tutorial: resume an object-summary corpus

1. Open [05.application.summary.object.ipynb](../../nbs/05.application.summary.object.ipynb).
   Use a kernel from the project environment and run the configuration cell first.
2. Set `OLLAMA_HOST` and `OLLAMA_SUMMARY_MODEL` in the repository `.env`.
   `OLLAMA_API_KEY` is optional. Review `INPUT_DIRS` before running discovery.
3. Keep `RESUME = True` and the existing `.object-summary-checkpoint.json` in
   each input directory. Run provider preflight, then the main corpus batch.
4. Inspect the result rows and completed-file display. A `failed` row contains
   its error; rerun the batch with the same configuration to retry missing work.

The notebook also has a remaining-corpus branch. Its current
`RUN_REMAINING_SUMMARY = True` enables moving unmatched Markdown into
`assets/md.json.semantics.remaining` before summarizing it. Review that setting
and the selected source/comparison directories before executing the branch; see
the [corpus tutorial](../corpus/README.md) for move semantics.

Completed outputs are checked for missing descendants and missing summaries,
including deeper table groups and list items. The runner requests only missing
summaries and preserves existing object, prose, section, and document summaries.
Additive code or object-schema changes alone do not require rebuilding completed
documents. Source/output signatures and model/settings compatibility checks
remain active. A completed document should be rebuilt only when explicitly
requested.

## Tutorial: call the object runner from a script

This example uses the existing corpus checkpoint and makes live model requests.
Keep its model, language, length thresholds, and generation settings consistent
with the notebook run whose checkpoint you are resuming. Save it as a script in
the repository root, or run it with that directory as the working directory.

```python
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from utils.summary.summary_asyncio import configure_summary_limits

load_dotenv(".env", override=False)
limits = configure_summary_limits(summary_module="object")

# Apply limits before importing any DOM summary modules.
from utils.summary.summary_object_asyncio import (
    discover_markdown,
    summarize_object_documents,
)
from utils.summary.summary_ollama_pool import OllamaSummaryPool


async def main():
    folder = Path("assets/code-docs.4.0/.md").resolve()
    model = os.environ["OLLAMA_SUMMARY_MODEL"]
    hosts = list(dict.fromkeys(
        host.strip().rstrip("/")
        for host in os.getenv("RIBOSOME_OLLAMA_HOSTS", os.environ["OLLAMA_HOST"]).split(",")
        if host.strip()
    ))
    api_key = os.getenv("OLLAMA_API_KEY")
    pool = OllamaSummaryPool(
        hosts=hosts,
        concurrency_per_server=int(os.getenv(
            "RIBOSOME_OLLAMA_SERVER_CONCURRENCY",
            str(max(1, limits["RIBOSOME_SUMMARY_CONCURRENCY"] // len(hosts))),
        )),
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        request_timeout=float(os.getenv("RIBOSOME_SUMMARY_REQUEST_TIMEOUT", "300")),
        request_retries=int(os.getenv("RIBOSOME_SUMMARY_REQUEST_RETRIES", "2")),
        max_tokens=int(os.getenv("RIBOSOME_SUMMARY_MAX_TOKENS", "2048")),
        think=False,
    )
    try:
        await pool.check_models(model)
        rows = await summarize_object_documents(
            discover_markdown(folder), pool, model,
            corpus=folder.name,
            concurrency=limits["RIBOSOME_DOCUMENT_CONCURRENCY"],
            checkpoint_path=folder / ".object-summary-checkpoint.json",
            checkpoint_host=hosts,
            resume=True,
            lang="zh", leaf_min_len=100, min_len=None,
        )
        for row in rows:
            print(row["status"], row["file"], row["error"])
        print(pool.metrics.snapshot())
    finally:
        await pool.aclose()


asyncio.run(main())
```

In a Jupyter cell, use `await main()` instead of `asyncio.run(main())`.
`discover_markdown` recursively returns sorted Markdown files and rejects missing
or empty folders. The object runner does not require a Chroma database.

For the natural-text pipeline, use its notebook or
`await summarize_documents(files, client, model, database, ...)`. It accepts a
`SummaryAsyncClient`, a DOM database client, and optional `checkpoint_path`,
`checkpoint_host`, and `resume=True`. Its notebook uses an ephemeral Chroma client
and `assets/md.json.semantics.summary/.summary-checkpoint.json`.

## Configuration and lifecycle

| Setting | Default and effect |
| --- | --- |
| `RIBOSOME_DOCUMENT_CONCURRENCY` | `4` documents in flight after `configure_summary_limits`. |
| `RIBOSOME_SUMMARY_CONCURRENCY` | `8` concurrent DOM summary requests. |
| `RIBOSOME_SUMMARY_TASK_LIMIT` | `32` scheduled summary tasks. Apply these limits before DOM imports; stale kernel settings cause an explicit restart error. |
| `OllamaSummaryPool.concurrency_per_server` | `4` slots per server at the API level. The object notebook computes its default from the global request limit and selected host count. |
| `request_timeout` / `request_retries` | Pool defaults: `300` seconds per attempt and `2` timeout retries. Local slot waiting is outside the inference deadline. |
| `max_tokens` / `think` | Pool defaults: `2048` output tokens and `False`; the client supplies `num_predict` and `think` unless the request already sets them. |
| `progress_interval` | Runner default: `0.25` seconds. |

The object notebook reads timeout/retry/token settings from
`RIBOSOME_SUMMARY_REQUEST_TIMEOUT`, `RIBOSOME_SUMMARY_REQUEST_RETRIES`, and
`RIBOSOME_SUMMARY_MAX_TOKENS`. `RIBOSOME_OLLAMA_HOSTS` selects a comma-separated
host list; its notebook default combines `http://172.27.74.49:11434` and the
configured `OLLAMA_HOST`, removing duplicates. Private services use direct
connections, following [ollama_config.py](../../ribosome/ollama_config.py).

`check_models(model)` checks every server's model list and requires the exact
model name on all servers. It performs no generation. `reset_metrics()` requires
an idle client. `aclose()` also requires outstanding requests to have finished or
been cancelled and awaited. A runner prevents overlapping batches in the same
process because it temporarily installs progress hooks.

## Outputs and recovery

- The object runner publishes an adjacent `<stem>_semantics.json` only after
  that document succeeds. Object extraction does not write AST caches.
- Completed rows are saved atomically in the selected checkpoint. The object
  runner keeps unpublished work in `<stem>_summary-progress.json`, separately
  from the last completed output and checkpoint.
- On failure or cancellation, completed output and completed checkpoint entries
  remain available for retry. Compatible partial work can preserve summaries
  already generated during the interrupted attempt.
- Result rows include `status`, `file`, `output`, `error`, elapsed time, and
  resume/incremental information. Client snapshots report request counts,
  latency, provider timing, throughput, and per-document/server activity.

## Tutorial: compare scheduling without changing corpus outputs

Run a mock benchmark first. It uses synthetic samples and simulated model calls:

```sh
.venv/bin/python utils/summary/benchmark_summary_asyncio.py \
  --output-dir /tmp/ribosome-summary-benchmark --repeats 1
```

For a live comparison, provide a configured host/model and an input directory
containing reusable `*_ast.json` samples:

```sh
.venv/bin/python utils/summary/benchmark_summary_asyncio.py --live \
  --input-dir assets/md.json.semantics.summary \
  --output-dir /tmp/ribosome-summary-live \
  --request-limit 8 --task-limit 32 --document-limit 4
```

The benchmark writes `samples.json`, variant outputs, and `report.json` under its
output directory, not over the source corpus. Without `--output-dir`, it creates
a temporary directory and prints its path. `--timeout-seconds` bounds each pass;
failures are recorded and cause a nonzero exit. `--server-parallel` only records
the server setting for the report; it does not reconfigure Ollama. See the
[scheduling design](../../docs/summary-asyncio-acceleration-plan.md) for context.

## Offline validation

These focused checks use fixtures and mocked providers:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest --confcutdir=tests -q \
  tests/test_summary_checkpoint_routing.py tests/test_summary_server_progress.py \
  tests/test_summary_ollama_pool.py
```

The broader summary suites cover batches, partial recovery, and nested objects.
Some broader async tests have known timeouts in this checkout, also reproduced
with the original helper implementation. Run them separately from the focused
checks; those focused checks do not exercise live provider workflows.
