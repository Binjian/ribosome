# Notebook inspection and maintenance

[All utility groups](../README.md)

Inspect nbdev source notebooks, apply specific historical notebook migrations,
and filter notebook output for Git. The `.ipynb` files under `nbs/` are the
source of truth for generated `ribosome` modules.

## Utilities

| Module | Interface | Purpose and files affected |
| --- | --- | --- |
| [notebook_cells.py](notebook_cells.py) | Python library | Find cells by declarations/headings/content and load evaluation question-bank declarations. |
| [nbstripout_filter.py](nbstripout_filter.py) | stdin/stdout command | Delegate supported notebook JSON to `nbstripout`; pass malformed or unsupported input through unchanged. |
| [update_embedding_notebook.py](update_embedding_notebook.py) | One-off migration | Replace three implementation cells in `nbs/01.core.dom.embedding.natural_text.ipynb` with the captured embedding correction. |
| [update_embedding_tests.py](update_embedding_tests.py) | One-off migration | Migrate fakes/assertions in `tests/test_embedding_fastcore.py`, `tests/test_dom_behavior.py`, and hidden tests in the natural-text embedding notebook. |
| [add_embedding_notebook_regressions.py](add_embedding_notebook_regressions.py) | One-off migration | Add/update the `embedding-replacement-regressions` cell in the natural-text embedding notebook. |
| [update_dom_function_tests.py](update_dom_function_tests.py) | One-off migration | Rewrite four manual test cells and insert/update `fresh-function-test-dom` in `nbs/01.core.dom.model.ipynb`. |

Commands below run from the repository root. The migration scripts use the
standard library and locate the checkout relative to their own files. Cell
inspection examples use `nbformat` from the project environment; Git filtering
also requires an executable named `nbstripout` on `PATH`.

## Tutorial: find a cell without relying on its ID

Cell IDs can change when notebooks are cleaned. Select the declaration you need
instead:

```python
from pathlib import Path
import nbformat
from utils.notebooks.notebook_cells import defined_names, find_cell

path = Path("nbs/06.test.rag.object.question_with_location.ipynb")
notebook = nbformat.read(path, as_version=4)
cell = find_cell(notebook, defines="EVALUATION_CONFIG_SOURCE")
print(sorted(defined_names(cell)))
print(cell.source)
```

This reads the notebook and parses Python declarations without executing its
cells or opening model services.

`find_cell(notebook, *, defines=(), heading=None, containing=None, required=True)`
combines any supplied selectors. `defines` accepts one name or several names;
`heading` matches the first line of a Markdown cell exactly; `containing` matches
text inside a code cell. Exactly one cell must match. `required=False` returns
`None` for no match, but still rejects ambiguous matches.

For example, select the evaluation notebook's top-level heading:

```python
heading = next(cell.source.splitlines()[0] for cell in notebook.cells
               if cell.cell_type == "markdown" and cell.source.startswith("# "))
title_cell = find_cell(notebook, heading=heading)
print(title_cell.source)
```

`defined_names` returns assignment bindings and function/class declarations
outside function/class bodies; it does not track imported names. Invalid Python
syntax, including cells containing unsupported notebook magic syntax, produces
an empty declaration set rather than executing anything.

### Load only the evaluation question banks

Continuing with the same `notebook`:

```python
from utils.notebooks.notebook_cells import load_question_banks

values = load_question_banks(notebook)
cases = (values["GENERAL_OPERATION_CASES"]
         + values["ARC_WELDING_CASES"]
         + values["SAMPLED_CASES"])
print(f"Loaded {len(cases)} cases")
```

Unlike `find_cell`, `load_question_banks` executes selected cell source. It
selects declarations/registrations for the three named banks and names matching
`*_SAMPLE_CASES`, in notebook order. It does not execute unrelated service setup
or generation cells. Use it with trusted project notebooks: any other statements
in a selected cell execute too. Pass an existing namespace as the optional
second argument if declarations depend on values you have supplied.

## Tutorial: filter a notebook into a separate file

For supported notebook JSON, the wrapper invokes the installed `nbstripout`
command and forwards its output. It does not edit the input file:

```sh
uv pip install --python .venv/bin/python nbstripout
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python utils/notebooks/nbstripout_filter.py \
  < nbs/01.core.dom.embedding.natural_text.ipynb \
  > /tmp/embedding.cleaned.ipynb
```

Do not redirect output over the same input path: shell redirection truncates it
before the wrapper reads it. Empty input, malformed JSON, merge-conflict text,
and unsupported notebook versions pass through byte-for-byte. Valid supported
notebooks fail with a nonzero status if `nbstripout` is missing; malformed input
can still pass through without that executable.

In an environment with pip installed, `python -m pip install nbstripout` is an
alternative; put that environment's executable directory on `PATH`.

### Optional repository-local Git clean filter

To explicitly configure this wrapper for Git in a checkout, run from its root
with `nbstripout` available on Git's `PATH`:

```sh
git config --local filter.nbstripout.clean 'python3 utils/notebooks/nbstripout_filter.py'
git config --local filter.nbstripout.smudge cat
git config --local filter.nbstripout.required true
```

The rule `*.ipynb filter=nbstripout` must also exist in `.gitattributes` or the
checkout's `.git/info/attributes`. Configuration alone does not apply a filter.
The clean filter changes the staged representation; it does not clean the
working notebook in place. Inspect existing filter configuration before
replacing it. Malformed notebook text remains inspectable/stageable for conflict
resolution because the wrapper deliberately preserves it.

## Tutorial: apply a historical embedding correction

These migration scripts capture a particular implementation revision. They
are not general upgrade commands and have no argument parser or dry-run mode:
even passing `--help` invokes their migration. Review their captured cell
sources and the current target cells before choosing to apply them, preferably
in a working copy where the resulting diff is easy to inspect.

For a checkout that needs that specific embedding correction:

```sh
python utils/notebooks/update_embedding_notebook.py
python utils/notebooks/update_embedding_tests.py
python utils/notebooks/add_embedding_notebook_regressions.py
git diff -- nbs/01.core.dom.embedding.natural_text.ipynb \
  tests/test_embedding_fastcore.py tests/test_dom_behavior.py
.venv/bin/nbdev-export --path nbs/01.core.dom.embedding.natural_text.ipynb
.venv/bin/nbdev-test --path nbs/01.core.dom.embedding.natural_text.ipynb
```

The first migration replaces cells identified by `7a1aeb88`, `076222a3`, and
`3850676e`. The regression installer uses a named ID and inserts its cell if
needed. Existing cell metadata/IDs are retained by the embedding scripts; JSON
serialization can change formatting. If IDs were regenerated or the notebook
has advanced beyond this revision, inspect and adapt the migration rather than
treating a successful exit as proof that the intended cells were updated.

The migrations edit source/test files without executing notebook cells.
`nbdev-export` generates Python modules; `nbdev-test` executes the eligible
notebook checks. Do not edit generated modules to make a source correction.
See [preparation profiles](../../docs/preparation-profiles.md) for exporting
without executing notebook workflows, and the
[embedding guide](../embedding/README.md) for current embedding operation.

### Refresh the manual DOM function tests separately

`python utils/notebooks/update_dom_function_tests.py` targets the model notebook,
not the embedding migration. It expects four specific cell IDs plus the
`# DomClass Function Tests` heading, and writes manual `# | notest` test cells.

Running the updater only rewrites notebook source. Later, executing its generated
manual tests deletes registered derived artifacts for the selected `SN024002`
and `SX022001` Markdown fixtures before rebuilding the AST; textualization tests
also regenerate their semantics and can use configured model services. This
fixture-specific workflow is separate from the object-summary application.
Keep `RESUME=True` and existing corpus checkpoints in
[05.application.summary.object.ipynb](../../nbs/05.application.summary.object.ipynb);
routine summary work does not call these fixture-reset helpers.

## Troubleshooting and validation

| Symptom | Meaning and next step |
| --- | --- |
| `Expected one notebook cell ... found 0` | The requested declaration/heading changed; inspect the current sources and choose a valid selector. |
| `... found 2` | The selector is ambiguous; add declarations or `containing` text. |
| Missing question banks | Supply the expected evaluation notebook and any required declaration namespace. |
| Migration makes no intended change or raises `StopIteration` | Its historical cell IDs/headings no longer match; inspect the source revision. |
| Missing `nbstripout` | Install it and put its executable on the command's `PATH`. |

Run the read/filter checks independently of notebook migrations:

```sh
.venv/bin/python -m pytest --confcutdir=tests -q \
  tests/test_notebook_cells.py tests/test_nbstripout_filter.py
```

The output-stripping test is skipped when the `nbstripout` executable is absent.
