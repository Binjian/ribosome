# Embedding utilities

[Utility index](../README.md) · [Embedding application notebook](../../nbs/05.application.embedding.object.ipynb) · [Object embedding design](../../docs/object-embedding-plan.md)

This group supplies an asynchronous Ollama embedding pool and an in-place
notebook status panel. Index construction and retrieval remain in the exported
`ribosome.core.dom.embedding.object` API; the application notebook combines them
into a corpus workflow.

## Module guide

| Module | Public helpers |
| --- | --- |
| [embedding_ollama_pool.py](embedding_ollama_pool.py) | `OllamaEmbeddingPool`, `HEALTH_CHECK_TEXT`: bounded request routing, inference health probes, retries, quarantine, recovery, and activity snapshots. |
| [embedding_progress.py](embedding_progress.py) | `EmbeddingProgressDisplay`, `render_embedding_status`: one HTML panel showing requests, servers, document results, and timings. Rendering makes no provider calls. |

These are importable Python helpers, not standalone CLIs. Use the qualified
imports shown below; the previous flat `utils` module paths have been removed.

## Prerequisites

Use the project environment with `ollama`, `httpx`, and the project's other
dependencies installed. The progress panel needs IPython; persistent indexing
also needs Chroma and the generated object-embedding module:

```sh
uv run ribosome-prepare --profile standalone
```

See [preparation profiles](../../docs/preparation-profiles.md). Run shell commands
and scripts below from the repository root. Live examples require the requested
embedding model on every server that preflight must accept; the helpers do not
download models. `OLLAMA_API_KEY`, when needed by the service, must be passed as
a bearer header by the caller.

## Tutorial: request an embedding with preflight and cleanup

This complete script checks a configured service and generates one live
embedding. It does not open or modify an index.

```python
import asyncio
import os

from dotenv import load_dotenv
from ribosome.ollama_config import OLLAMA_EMBED_HOST
from utils.embedding.embedding_ollama_pool import OllamaEmbeddingPool

load_dotenv(".env", override=False)


async def main():
    model = os.getenv("OLLAMA_EMBED_MODEL", "qwen3-embedding:8b")
    api_key = os.getenv("OLLAMA_API_KEY")
    pool = OllamaEmbeddingPool(
        hosts=[OLLAMA_EMBED_HOST],
        concurrency_per_server=4,
        headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        request_timeout=300,
    )
    try:
        await pool.check_models(model, require_all=True)
        response = await pool.embed(model=model, input="How do I set the tool frame?")
        print(response)
        print(pool.status_snapshot())
    finally:
        await pool.aclose()


asyncio.run(main())
```

In a Jupyter notebook, use `await main()` instead of `asyncio.run(main())`.
`embed` returns the provider's original response, preserving fields such as the
reported model and embeddings. It accepts the usual Ollama embedding keyword
arguments in addition to the required `model` and `input`.

`check_models` checks model metadata and actually embeds `HEALTH_CHECK_TEXT` to
verify inference and a usable finite, nonzero vector. Its default
`require_all=False` permits a healthy subset of servers; `True` requires every
configured host to pass. Await preflight before submitting work.

## Tutorial: index a selected corpus in the notebook

1. Open [05.application.embedding.object.ipynb](../../nbs/05.application.embedding.object.ipynb)
   with the project kernel and run its configuration cell.
2. Review `EMBEDDING_MODE`. The current default is `"remaining"`, which reads
   `assets/md.json.semantics.remaining/*_semantics.json` recursively. `"object"`
   selects semantics files under the 4.0/5.0 corpus folders; `"task"` selects
   `*_tasks.json` there instead.
3. Review `OLLAMA_SERVER_MODE`: `"16"`, `"49"`, or the current default `"both"`.
   These select the explicit endpoints in
   [ollama_config.py](../../ribosome/ollama_config.py):
   `http://172.27.74.16:11434` and `http://172.27.74.49:11434`.
4. Keep the intended existing `INDEX_DIR` and `FORCE_REBUILD=False`. Run discovery
   and preflight. Discovery rejects missing folders or an empty selected input
   pattern; preflight requires every selected server.
5. Run the indexing cell and inspect its result rows. The status panel refreshes
   while an individual document's requests are in flight, not just between files.

`ObjectEmbeddingIndex` writes a SQLite catalog at
`INDEX_DIR/catalog.sqlite3` and vectors in `INDEX_DIR/chroma/`. The collection is
`object_summaries` for object/remaining modes and `task_embeddings` for task mode.
For remaining mode, the index root is
`db/object_embeddings/md.json.semantics.remaining/`; other modes use
`db/object_embeddings/` or `db/task_embeddings/`. The final directory name includes
the model and sorted selected host addresses.

The index can return `indexed`, `unchanged`, or `failed` document results.
Rerunning the same inputs against the same compatible index reuses unchanged
work. The model, provider configuration, and collection define index identity;
worker limits do not. If the selected provider set or model changes, use the
corresponding index instead of opening an incompatible vector space. Source
semantics/task files and summary checkpoints are not rewritten by indexing.

Changing `FORCE_REBUILD=True` in the notebook creates a separate timestamped
index; it is an explicit fresh-index action, not part of normal resume.

## Tutorial: add a notebook status panel to your own loop

After the notebook has created `ollama_client`, `object_index`, and the selected
`corpora`, the display accepts result dictionaries or `IndexingResult` objects.
Use this Jupyter cell to replace the batch loop or to demonstrate an unchanged
rerun against that existing index:

```python
import asyncio
from utils.embedding.embedding_progress import EmbeddingProgressDisplay

pool = ollama_client
files = [file for _, corpus_files in corpora for file in corpus_files]
report = []
monitor = EmbeddingProgressDisplay(pool, total=len(files))

for file in files:
    monitor.update(current_file=file, report=report)
    task = asyncio.create_task(object_index.index_file(file))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.25)
            monitor.update(current_file=file, report=report)
        report.append(await task)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        monitor.update(current_file=file, report=report)

monitor.update(report=report, state="Completed")
```

This cell extends the notebook's existing pool/index setup; it is not a complete
index initializer. The application notebook additionally catches per-document
errors, records failed rows, and finalizes the panel on cancellation.
`render_embedding_status` provides the HTML string directly when a custom
renderer is needed.

## Configuration defaults

The pool constructor accepts these options. The environment variable column
describes how the application notebook supplies them; the helper itself does
not automatically load `.env`.

| Pool option | Pool default | Notebook setting |
| --- | --- | --- |
| `hosts` | Required list of distinct HTTP(S) URLs | `OLLAMA_SERVER_MODE` selects explicit endpoints; an ambient `OLLAMA_HOST` does not replace them. |
| `concurrency_per_server` | `1` | `RIBOSOME_EMBED_SERVER_CONCURRENCY`, default `4`. |
| `request_timeout` | `300.0` seconds per attempt | `RIBOSOME_EMBED_REQUEST_TIMEOUT`, default `300`. |
| `request_retries` | `2` additional attempts | `RIBOSOME_EMBED_REQUEST_RETRIES`, default `2`. |
| `quarantine_seconds` | `30.0` | `RIBOSOME_EMBED_QUARANTINE_SECONDS`, default `30`. |
| `health_timeout` | `60.0` seconds per probe | `RIBOSOME_EMBED_HEALTH_TIMEOUT`, default `60`. |
| `retry_backoff` | `1.0` second base, exponentially bounded at 30 seconds | Constructor option; the notebook uses this default. |

The notebook's model defaults to `qwen3-embedding:8b` and can be set with
`OLLAMA_EMBED_MODEL`. Its current two-host selection and four slots per host allow
eight concurrent embedding requests. Environment values take precedence when
`.env` is loaded with `override=False`. Connections follow
`OLLAMA_TRUST_ENV=False`, so private services bypass environment HTTP proxies.

## Failures, progress, and cleanup

Transient transport/timeouts and HTTP 408/429/500/502/503/504 quarantine a server.
Retries are bounded and prefer another available host. After its cooldown, a
quarantined server must pass a recovery probe before receiving normal work.
A single-host pool waits for that recovery opportunity too. Nontransient errors
stop that request without a retry.

The inference deadline starts after acquiring local capacity; it includes the
server's queue/inference time. `status_snapshot()` reports running and queued
requests, configured and healthy capacity, cumulative outcomes, request ages,
and per-server health. Failed attempts and failed documents are separate counts
in the display: a retried request can still produce an indexed document.

Cancel and await outstanding tasks before `await pool.aclose()`. Closing during
requests or health probes raises an error. A closed pool must be recreated, and
preflight rerun, before new work. Close the `ObjectEmbeddingIndex` separately
with `object_index.close()` when finished with the catalog.

## Offline validation

The pool and display tests use mocked providers and do not embed a real corpus:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest --confcutdir=tests -q \
  tests/test_embedding_ollama_pool.py tests/test_embedding_progress.py \
  tests/test_embedding_object_application.py tests/test_object_embedding.py
```

For the natural-text embedding API, see
[01.core.dom.embedding.natural_text.ipynb](../../nbs/01.core.dom.embedding.natural_text.ipynb)
and import `ribosome.core.dom.embedding.natural_text.embed`. It is distinct from
the object-index helper workflow described here.
