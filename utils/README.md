# Repository utilities

Utilities are organized by workflow. Each group has its own module reference,
prerequisites, tutorials, configuration notes, and troubleshooting guide.

## Choose a guide

| Group | Use it to… | Tutorial starting point |
| --- | --- | --- |
| [Corpus files](corpus/README.md) | Compare Markdown collections, move the remaining documents with their images, and copy matching source PDFs. | Preview a corpus difference or PDF copy before writing files. |
| [Deployment](deployment/README.md) | Install and manage the Baidu Unlimited OCR 2 inference service on a Linux NVIDIA host. | Preview deployment, install the service, and connect a notebook client. |
| [Embedding](embedding/README.md) | Pool Ollama embedding requests and display notebook indexing progress. | Check configured servers, embed a batch, then run the application notebook. |
| [KRL collection](krl/README.md) | Collect KUKA Center chatbot answers into resumable Markdown batches. | Inspect pending questions offline, collect one answer, then resume. |
| [Notebook tools](notebooks/README.md) | Locate notebook cells, filter outputs for Git, or apply a specific historical source migration. | Inspect cells and question banks without running service workflows. |
| [RAG evaluation](rag/README.md) | Build/audit question samples and run, review, and publish object/task retrieval evaluations. | Prepare query embeddings, run an evaluation, review answers, and publish results. |
| [Summarization](summary/README.md) | Run async summarization with checkpoints, server pools, progress, and scheduling benchmarks. | Resume existing object summaries or compare schedulers with a mock benchmark. |

```text
utils/
├── corpus/       # Corpus selection and source-file copying
├── deployment/   # Inference service installation and management
├── embedding/    # Embedding pools and progress displays
├── krl/          # Chatbot collection and Markdown batch storage
├── notebooks/    # Cell inspection, Git filtering, and source migrations
├── rag/          # Sample banks, retrieval evaluation, and answer review
└── summary/      # Async summarization, checkpoints, pools, and benchmarks
```

## Set up a checkout

Run repository commands from the directory containing `pyproject.toml`. For the
full project environment, use the existing lock file and prepare notebook-authored
Python exports:

```sh
uv sync --locked
uv run ribosome-prepare --profile standalone
```

The project requires Python 3.12 or newer. The standalone OCR host installer has
its own Python and system requirements described in the deployment guide.
Some smaller tools use only the standard library; the KRL collector needs
`requests` for live collection, and notebook output filtering needs the
`nbstripout` executable. Each group lists its own requirements.

Preparation exports code without executing notebook workflows or rebuilding
corpora. Use `--profile llm-backend` for the narrower backend export set; see
[preparation profiles](../docs/preparation-profiles.md) for alternate destinations
and packaging. Runtime helpers remain authored in `nbs/`, while these utilities
are handwritten checkout tools. The `utils` packages are not included in the
installed `ribosome` distribution.

Commands that use `assets/`, `db/`, local indexes, `.env`, or model services need
those resources in your checkout. They are not created merely by importing a
utility or exporting notebook code. Check the relevant tutorial before starting
a workflow.

## Run tools and import helpers

Commands in the guides use `.venv/bin/python` where project dependencies are
needed. A documented CLI supports both its file path and its module entry point:

```sh
.venv/bin/python utils/krl/collect_krl_qa.py --dry-run
.venv/bin/python -m utils.krl.collect_krl_qa --dry-run
```

Use module entry points from the repository root, or put the checkout on
`PYTHONPATH`. CLIs with repository-based defaults resolve those defaults from
their script location; custom relative input/output paths still depend on the
current working directory. The deployment guide also covers copying its
standalone installer to another host.

Import libraries through their group, for example:

```python
from utils.notebooks.notebook_cells import find_cell
from utils.corpus.markdown_corpus import markdown_difference
from utils.krl.krl_collection_store import CollectionStore
```

The old flat imports and commands, such as `utils.summary_asyncio` and
`utils/collect_krl_qa.py`, have moved to `utils.summary.summary_asyncio` and
`utils/krl/collect_krl_qa.py`. The group guides list every current module.

Only tools documented as CLIs have argument parsers. In particular, the notebook
migration scripts perform their edits when invoked and do not implement `--help`
or `--dry-run`; review their historical targets in the notebook tools guide.

## Outputs and resumed work

The guides identify what each workflow reads and writes. KRL answers, summary
checkpoints, RAG evaluation results, and embedding indexes have separate resume
and compatibility rules; moving the utilities does not require regenerating
those artifacts.

For [object-summary batch processing](summary/README.md), keep `RESUME=True` and
reuse existing corpus checkpoints. Feature additions fill missing descendants
and summaries while preserving completed object, prose, section, and document
summaries. Rebuild completed documents only when explicitly requested. Existing
source/output signatures and model/settings compatibility checks remain active.

## Validate changes

Each group guide includes focused validation commands. After preparing runtime
exports, the CLI relocation checks can be run independently:

```sh
.venv/bin/python -m pytest --confcutdir=tests -q tests/test_utils_entrypoints.py
```

`--confcutdir=tests` avoids the root test session's automatic export hook and
assumes the needed runtime modules are already prepared. For the full project
suite, `uv run pytest` uses that hook to prepare ignored notebook exports.
Live model calls, chatbot login, service deployment, and corpus migrations are
separate workflows described in their guides.
