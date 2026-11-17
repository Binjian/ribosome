For standalone or llm-backend package exports, use `uv run ribosome-prepare
--profile standalone` or `--profile llm-backend`. See
[preparation profiles](../docs/preparation-profiles.md) for defaults, environment
configuration and packaging.

To collect the original KUKA Center chatbot answers in **KRL编程** mode:

```sh
python -m pip install requests
python utils/collect_krl_qa.py --dry-run
python utils/collect_krl_qa.py
```

The default account is `13918961550`. Enter the password at the hidden prompt,
or provide it with the `KUKA_PASSWORD` environment variable. The script also
prompts for acceptance of the site's login policies and a four-character image
CAPTCHA: open the printed image path, read the code, and enter it in the terminal.
Passwords and authentication cookies are kept in memory. `--username` (or
`KUKA_USERNAME`) selects another account. `--accept-login-terms` explicitly
accepts the linked login policies without the consent prompt; CAPTCHA entry is
still required.

The collector reads every full prompt from `assets/KRL/krl_questions_1000.md`,
starting at **0019 真空搬运**, and skips previously saved pairs. It preserves the
existing 0001–0018 answers, adds 0019–0020 to `assets/KRL/krl_qa_collected.md`,
then writes 0021–0040 to `krl_qa_collected_002.md`, and continues with 20 pairs per
file. The final batch can contain fewer pairs. Existing introductory text stays
unchanged, including its historical count of manually collected answers.

Each question uses its own chat conversation and the site's KRL model. The
collector retains the full original Markdown, including code fences, tables,
and any reasoning/reference sections supplied by the service. It waits for
stream EOF, applies the site's answer check, and verifies the persisted answer
against the stream before atomically publishing a pair. A site refusal is saved
as the refusal shown by the frontend. Interrupted or inconsistent responses are
not published. On errors, quota limits, or Ctrl+C, rerun the same command to
resume from the first missing pair. An interrupted remote question can be asked
again; completed local pairs are never requested again.

Use `--max-questions 1` for an initial live check, `--end 40` to stop at a sample,
and `--delay`, `--request-timeout`, or `--answer-timeout` to adjust pacing and
timeouts. `--questions` and `--output` select other paths. The collector holds a
process lock on the output series, validates saved prompts and new answer
integrity markers, and stops on conflicts. It uses the site's frontend HTTP
endpoints, which may change; it stops if their response contracts differ.

Offline validation (no login or chatbot requests):

```sh
python -m pytest --confcutdir=tests -q tests/test_krl_collection_store.py tests/test_collect_krl_qa.py
```

To copy source PDFs corresponding to `assets/md.json.semantics.remaining` into
`assets/pdf.remaining`, preserving the source folders:

```sh
python utils/copy_remaining_pdfs.py --dry-run
python utils/copy_remaining_pdfs.py
```

The script matches relative paths, including the extra document folder created
by Markdown conversion. Documents without a corresponding PDF are reported and
skipped. Reruns reuse identical copies and reject conflicting destination files.
Use `--markdown-dir`, `--pdf-dir`, or `--output-dir` to select other directories.

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

The application notebook defaults to `qwen3-embedding:8b` with the async
`OllamaEmbeddingPool` in `embedding_ollama_pool.py`. Embedding requests use
`http://172.27.74.49:11434` from `ribosome.ollama_config`, with four concurrent
requests by default. Optional RAG generation uses `http://172.27.74.16:11434`.
Host environment settings do not override these notebook endpoints. Preflight
requires the embedding model on the configured server. Set `OLLAMA_EMBED_MODEL`,
`RIBOSOME_EMBED_SERVER_CONCURRENCY`, and `RIBOSOME_EMBED_REQUEST_TIMEOUT` in the
project `.env` to configure these settings; existing environment values take
precedence. Corpus indexing, the
live concurrency display, optional fresh-index rebuilds, catalog inspection, and
evidence retrieval are all in the application notebook. The core notebook contains
the reusable API and an offline functional test. The workflow uses
`USE_TASKS=True` by default, selecting `*_tasks.json` and
`db/task_embeddings/qwen3-embedding-8b-172.27.74.49-11434/`. Set `USE_TASKS=False`
for `*_semantics.json` and the corresponding directory under `db/object_embeddings`.
These host-specific directories preserve earlier indexes.
The provider host set and model identify the vector space; worker limits do not.

###

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

## Baidu Unlimited OCR 2 host deployment

[`deploy_unlimited_ocr_v2.py`](deploy_unlimited_ocr_v2.py) is a standalone Python
3.10+ installer. Run it on the Linux NVIDIA GPU host as the service account,
without sudo. It installs Baidu's checksum-pinned custom SGLang wheel in a
separate venv, downloads a pinned model revision, creates a private API key and
client configuration, and starts an authenticated user systemd service:

```sh
python3 utils/deploy_unlimited_ocr_v2.py deploy --dry-run --client-host 172.27.74.16
python3 utils/deploy_unlimited_ocr_v2.py deploy --gpu 0 --port 7870 --client-host 172.27.74.16 --smoke-test
```

The default install directory is `~/services/unlimited-ocr-v2`. Existing settings
and credentials are preserved; supplied options override the corresponding
settings. Stop an existing instance before redeploying. `--no-start` prepares the
environment and user unit without starting it; `--no-systemd` prepares a manual
deployment instead. `--skip-gpu-check` requires one of those preparation modes.

Actions include `run`, `check --smoke-test`, `status`, `start`, `restart`, `stop`,
and `logs`. `client-env --client-host HOST` prints notebook settings **including
the API key**; a private `client.env` is also saved in the install directory.
Copy those settings into the existing project `.env` without replacing its other
values. The script does not change drivers, other inference services, firewall
rules, notebook environments, or corpus checkpoints. User-service persistence
after logout requires administrator-enabled lingering.

See the [installation tutorial](../docs/baidu-unlimited-ocr-v2-upgrade-plan.md#tutorial-install-and-run-the-sglang-service)
for prerequisites, transfer-to-host commands, service management, and tuning.
