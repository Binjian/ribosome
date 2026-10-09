These Python scripts preserve the notebook cell IDs and metadata while applying
this embedding correctness change. They resolve the project root from their own
location and can run from any working directory:

```sh
python utils/update_embedding_notebook.py
python utils/update_embedding_tests.py
python utils/add_embedding_notebook_regressions.py
.venv/bin/nbdev-export --path nbs/01.core.dom.embedding.ipynb
.venv/bin/nbdev-test --path nbs/01.core.dom.embedding.ipynb
```

They are idempotent for this implementation revision. Review before reusing after
future notebook changes: the first script replaces the three implementation
cells with the captured version. The notebook remains the production source of
truth; these scripts are development utilities, not runtime dependencies.
