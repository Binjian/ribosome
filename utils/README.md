These Python scripts preserve the notebook cell IDs and metadata while applying
this embedding correctness change. They resolve the project root from their own
location and can run from any working directory:

```sh
python utils/update_embedding_notebook.py
python utils/update_embedding_tests.py
python utils/add_embedding_notebook_regressions.py
.venv/bin/nbdev-export --path nbs/01.core.dom.embedding.natural_text.ipynb
.venv/bin/nbdev-test --path nbs/01.core.dom.embedding.natural_text.ipynb
```

They are idempotent for this implementation revision. Review before reusing after
future notebook changes: the first script replaces the three implementation
cells with the captured version. The notebook remains the production source of
truth; these scripts are development utilities, not runtime dependencies.

The natural-text notebook now exports `ribosome.core.dom.embedding.natural_text`.
Import the existing interfaces directly from that module, for example
`from ribosome.core.dom.embedding.natural_text import embed`. The embedding
package initializer does not re-export the natural-text interfaces.

For recursive object embeddings, use `01.core.dom.embedding.object.ipynb` and
`ribosome.core.dom.embedding.object.ObjectEmbeddingIndex`. Its manual examples
index `*_semantics.json` into a dedicated Chroma collection and SQLite catalog,
then retrieve summaries with raw source content and parent/child context. The
implementation plan is in [object-embedding-plan.md](../docs/object-embedding-plan.md).
For folder-based batch processing with preflight, progress, failure reports, and
catalog inspection, run [05.application.embedding.object.ipynb](../nbs/05.application.embedding.object.ipynb).
The application notebook defaults to `*_tasks.json`, the `task_embeddings`
collection, and `qwen3-embedding:8b`. Repository deployment endpoints are shared
in `ribosome/ollama_config.py`: embeddings use only `http://172.27.74.49:11434`;
RAG answers and evaluation judging use only `http://172.27.74.16:11434`.
Legacy host environment variables cannot override the applications' split.
Summary routing and checkpoint compatibility remain configured separately.

The async `OllamaEmbeddingPool` checks model metadata and a synthetic embedding
before indexing. Transient failures get two retries with exponential backoff;
a failed host is quarantined for 30 seconds and must pass a single recovery probe
before its capacity reopens. The 300-second inference deadline starts after a local
slot is available. The panel shows health, retries, probes, queued work, request
outcomes and separate document failures. Cancellation awaits requests and preserves
completed publications and reusable cached vectors.

Models, optional authentication and worker limits remain configurable through
`OLLAMA_EMBED_MODEL`, `OLLAMA_API_KEY`, `RIBOSOME_EMBED_SERVER_CONCURRENCY` (four),
`RIBOSOME_EMBED_REQUEST_TIMEOUT` (300), `RIBOSOME_EMBED_REQUEST_RETRIES` (two),
`RIBOSOME_EMBED_QUARANTINE_SECONDS` (30), and `RIBOSOME_EMBED_HEALTH_TIMEOUT` (60).
The default task index is
`db/task_embeddings/qwen3-embedding-8b-172.27.74.49-11434/`.
`USE_TASKS=False` selects semantic inputs, `object_summaries`, and the corresponding
host-specific directory under `db/object_embeddings/`, which is also the RAG and
evaluation default. Existing indexes retain their original fingerprints. A new
host configuration requires a separate index; subsequent runs of that configuration
resume with `FORCE_REBUILD=False`. The host set and model identify the vector space;
worker limits and temporary health quarantine do not. Historical comparison paths
must be replaced with compatible `.49` indexes before rerunning the comparison.
Offline validation:

```sh
.venv/bin/nbdev-export
.venv/bin/nbdev-test --path nbs/01.core.dom.embedding.object.ipynb
.venv/bin/pytest -q tests/test_object_embedding.py
```

Object RAG evaluation lives in
[06.test.rag.object.question_with_location.ipynb](../nbs/06.test.rag.object.question_with_location.ipynb). The application
notebook retains the category demonstration; the evaluation notebook contains
the two relocated manual suites and 20 additional questions per CSV source.
Run from the repository root:

```sh
.venv/bin/python utils/run_object_rag_evaluation.py run --concurrency 2
.venv/bin/python utils/review_object_rag_evaluation.py --host http://172.27.74.16:11434 --concurrency 2 --watch
.venv/bin/python utils/run_object_rag_evaluation.py publish
```

The review command can run in a second terminal while answers are being generated.
It uses a separate source-based judge and labels its assessments as model-assisted.
By default the qualitative review covers all 68 original cases and three different
tasks per CSV file (182 cases across all 38 files). All 828 retrievals and answers
are still executed and saved; unreviewed cases receive no correctness grade.
Use `--all-cases` for exhaustive model-assisted grading.
The runner first batches and caches query embeddings from the configured model;
the cache is isolated by model, provider settings, endpoint and vector dimension.
To prepare vectors independently, use the `prepare` stage. A concurrent runner
can consume that cache with `run --skip-prepare`; missing vectors are embedded
on demand. This cache does not write to the corpus index.
The runner also memoizes read-only catalog snapshots while active generations and
catalog settings remain fixed. It preserves record order and category-specific
coverage, and fails explicitly if the index changes during a run.
`publish` reads compatible saved results, updates notebook outputs and the review,
and makes no model calls. Individual results and raw responses are saved atomically
under `db/object_rag_tests/results/`; re-running the same configuration resumes
completed stages and retries errors. Source references and expected facts are
never appended to production answer prompts. The existing index is opened for
retrieval, without rebuilding corpus summaries or checkpoints.

The committed notebook banks are sufficient to run the evaluation. To regenerate
them locally from the CSV and deterministic sampling rules, use `author-offline`
and then `build`; this recreates the appended sections, so publish recorded results
again afterward. The short load-analysis manual uses explicitly curated distinct
tasks, recorded in its sampling manifest. `OBJECT_RAG_INDEX` and the existing
Ollama environment settings control the index and services.
