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
Both notebooks default to `qwen3-embedding:8b` with the async
`OllamaEmbeddingPool` in `embedding_ollama_pool.py`. Requests use
`http://localhost:11434` and `http://172.27.74.16:11434` concurrently, with four
requests per server (eight total). Preflight requires the model on every configured host.
Set `OLLAMA_EMBED_HOSTS` (comma-separated), `OLLAMA_EMBED_MODEL`,
`RIBOSOME_EMBED_SERVER_CONCURRENCY`, and `RIBOSOME_EMBED_REQUEST_TIMEOUT` in the
environment or project `.env` to override these settings. Both workflows use
`db/object_embeddings/qwen3-embedding-8b/` by default, preserving earlier indexes.
The provider host set and model identify the vector space; worker limits do not.
Offline validation:

```sh
.venv/bin/nbdev-export
.venv/bin/nbdev-test --path nbs/01.core.dom.embedding.object.ipynb
.venv/bin/pytest -q tests/test_object_embedding.py
```
