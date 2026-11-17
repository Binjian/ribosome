# RAG evaluation sample banks

These supplemental Chinese question banks follow the category-only and task
question notebooks. Questions contain technical names and commands; source
addresses, revisions, excerpts, and expected facts stay in reviewer metadata.

| Bank | Questions | Evaluator |
| --- | ---: | --- |
| [regular_object_rag_3000.json](regular_object_rag_3000.json) | 3,000 | `CategoryEvaluationRun` |
| [program_rag_1000.json](program_rag_1000.json) | 1,000 | `TaskEvaluationRun` |

The banks are generated deterministically from local source material. Expected
facts are source quotations, not independently adjudicated model answers. No
retrieval, embedding, or answer-generation run is included in these artifacts.
The original notebooks, their question banks, and saved evaluation runs are
preserved.

The regular bank covers **18 categories and 36 source documents** across corpus
profiles 4.0 and 5.0. Its questions target instruction syntax and behavior,
parameters, operating procedures, prerequisites, and alarms.

The program bank covers **126 source tasks** (105 distinct task contents) in
17 documents and nine categories. Its 1,000 questions test command roles,
action-to-command mapping, instruction order, parameter values/comparisons,
branch conditions, documented constraints, and signal waits. Categories follow
available task evidence: 684 questions concern arc welding. These are multiple
questions per task, not 1,000 independent programs.

## Load and inspect

From the `vendors/ribosome` project directory:

```python
from pathlib import Path
from utils.rag.rag_evaluation_samples import load_sample_bank, audit_summary

regular = load_sample_bank("evaluation_samples/regular_object_rag_3000.json")
program = load_sample_bank("evaluation_samples/program_rag_1000.json")
print(audit_summary(regular))
print(audit_summary(program))
print(regular["cases"][0]["question"])
print(program["cases"][0]["question"])
```

Each `cases` entry includes the question, category, source profile, stable case
ID, and evidence references. Program samples additionally preserve the exact
`task_id`, `task_object_id`, and `task_content_digest` used by task retrieval
metrics. Multiple questions deliberately target different aspects of the same
task; this supplemental format is separate from the exhaustive one-question-per-
task bank. Use `load_sample_bank`, not `load_task_bank`, for these files.

## Use the existing evaluators

In `06.test.rag.object.question_with_category.ipynb`, after its imports, app,
embedding cache, and evaluation setup, run a separate sample evaluation:

```python
from utils.rag.rag_evaluation_samples import load_sample_bank

sample_bank = load_sample_bank(SAMPLE_DIR / "regular_object_rag_3000.json")
sample_cases = sample_bank["cases"]
sample_evaluation = type(evaluation)(
    app, sample_cases, RESULTS_DIR / "regular_object_rag_3000",
    top_k=EVALUATION_TOP_K, settings=evaluation.settings,
)
await query_embeddings.prepare([case["question"] for case in sample_cases])
metrics = await sample_evaluation.run(sample_cases, concurrency=2)
```

For program evaluation, use `05.application.rag.task.ipynb`. Replace its
question-loading cell with:

```python
from utils.rag.rag_evaluation_samples import load_sample_bank

bank = load_sample_bank(SAMPLE_DIR / "program_rag_1000.json")
cases = select_cases(bank["cases"], SELECTED_CASE_IDS)
```

Keep the task index coverage preflight (`task_index_coverage` and
`require_task_coverage`) in the index-opening cell. Set the `TaskEvaluationRun`
result directory to `RESULTS_DIR / "program_rag_1000"`. Continue with the existing
query-cache, retrieval, answer, and cleanup cells. Only `question`
and `category` are query inputs; never append gold references or expected facts
to generation prompts.

In these snippets, define `SAMPLE_DIR` as this directory and `RESULTS_DIR` as a
writable results directory. In the llm-backend checkout, the corpus is linked from the workspace
root, while these banks and utilities live under `vendors/ribosome`. Add that
ribosome directory to `sys.path` when importing the utilities from elsewhere.

## Verify and regenerate

From the llm-backend workspace root, the following audit runs without model
services. `--source-root .` additionally checks source file SHA-256 hashes and
regular-question excerpts against their exact Markdown line ranges, and matches
program task IDs and complete task contents against the saved task JSON:

```sh
python vendors/ribosome/utils/rag/rag_evaluation_samples.py \
  vendors/ribosome/evaluation_samples/regular_object_rag_3000.json \
  vendors/ribosome/evaluation_samples/program_rag_1000.json \
  --source-root .
```

The source corpus must be available to regenerate these files:

```sh
.venv/bin/python vendors/ribosome/utils/rag/generate_regular_object_samples.py \
  --source-root . --count 3000 \
  --output vendors/ribosome/evaluation_samples/regular_object_rag_3000.json
.venv/bin/python vendors/ribosome/utils/rag/generate_program_rag_samples.py \
  --source-root . --count 1000 \
  --output vendors/ribosome/evaluation_samples/program_rag_1000.json
```

## MongoDB collections

From the llm-backend workspace root, the importer reads `.env` and uses
`MONGODB_URI` and `MONGO_DB` (database default: `iqa`):

```sh
.venv/bin/python utils/import_rag_evaluation_samples.py --dry-run
.venv/bin/python utils/import_rag_evaluation_samples.py
.venv/bin/python utils/import_rag_evaluation_samples.py --verify-only
```

The default collections are `regular_object_rag_3000` and `program_rag_1000`.
Each question is one document with `_id = id`; evidence references, expected
facts, and task identities retain their native nested structure. Documents also
record the source-bank path/hash, source order, dataset type, and import dates.
`rag_evaluation_sample_banks` stores each bank's header metadata in `header_json`
(decode with `json.loads` to recover the original profile-keyed mappings). Stable-ID
upserts make repeat imports duplicate-free and preserve unrelated records and
annotations. Verification reads back every case and compares its source fields.

Read questions by category without modifying the database:

```python
import os
from dotenv import load_dotenv
from pymongo import MongoClient
from llm_backend.client_sdk.mongo_config import DEFAULT_MONGODB_URI

load_dotenv(".env", override=False)
storage_fields = {"_id", "dataset_type", "source_bank", "source_bank_sha256",
                  "source_order", "created_at", "updated_at"}
with MongoClient(os.getenv("MONGODB_URI", DEFAULT_MONGODB_URI)) as client:
    db = client[os.getenv("MONGO_DB", "iqa")]
    documents = db.regular_object_rag_3000.find({"category": "arc_welding"}).sort("source_order", 1)
    cases = [{key: value for key, value in document.items() if key not in storage_fields}
             for document in documents]
```

Use `db.program_rag_1000` for program questions. The resulting cases can be
passed to the corresponding evaluator; reviewer evidence remains separate from
the question and category sent for retrieval.

Keep all questions from the same task/content together when making dataset
splits: related questions and mirrored corpus revisions are not independent
examples. Inspect results by category, profile, and intent as well as overall;
question counts do not imply the same number of distinct programs.
