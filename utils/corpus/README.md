# Corpus utilities

These tools prepare source files for the notebook pipelines. They compare
Markdown by relative path, collect documents that are absent from other corpora,
and copy the original PDFs corresponding to a remaining Markdown corpus.

Run commands from the repository root. The utilities use only Python's standard
library; library imports require the repository root on `sys.path`.

| Module | Interface | Purpose |
| --- | --- | --- |
| [markdown_corpus.py](markdown_corpus.py) | `markdown_difference`, `move_markdown_difference` | Select and collect Markdown absent from one or more comparison corpora. |
| [copy_remaining_pdfs.py](copy_remaining_pdfs.py) | CLI and `copy_remaining_pdfs` | Copy the corresponding original PDFs while preserving source folders. |
| [move_remaining_office_markdown.py](move_remaining_office_markdown.py) | CLI and `move_remaining_office_markdown` | Relocate Markdown without a corresponding PDF. |

## Tutorial: preview the remaining Markdown

The comparison key is the complete path beneath each corpus root. A matching
relative path excludes a document even when its contents differ; a matching
basename in another folder does not. Both `.md` and `.MD` files are included.

```python
from pathlib import Path
from utils.corpus.markdown_corpus import markdown_difference

source = Path("assets/md.json.semantics.summary")
comparisons = [
    Path("assets/code-docs.4.0/.md"),
    Path("assets/code-docs.5.0/.md"),
]

remaining = markdown_difference(source, comparisons)
print(f"Remaining documents: {len(remaining)}")
for path in remaining[:20]:
    print(path.relative_to(source.resolve()))
```

This call reads directory entries without changing files. `comparison_dirs` can
also be one path or an iterable of paths. Empty directories are valid; missing
source or comparison directories raise `FileNotFoundError`.

## Tutorial: collect that selection into a separate corpus

After reviewing the selection, use a destination separate from the source and
every comparison directory. The helper rejects overlapping parent/child roots.
This step **moves the selected Markdown** out of the source:

```python
from pathlib import Path
from utils.corpus.markdown_corpus import move_markdown_difference

documents = move_markdown_difference(
    Path("assets/md.json.semantics.summary"),
    [Path("assets/code-docs.4.0/.md"), Path("assets/code-docs.5.0/.md")],
    Path("assets/md.json.semantics.remaining"),
)
print(f"Destination documents available for processing: {len(documents)}")
```

The helper preserves relative folders and copies image resources found beneath
each selected document's directory, including its image subfolders. Source
images remain available to other documents. Image copying is based on supported
file suffixes, rather than only on links parsed from the Markdown.

Existing identical destination files are reused. All destination content
conflicts are checked before any copy or move starts. Each file is copied to a
temporary file and published atomically; its source Markdown is removed only
after publication succeeds. This is per-file protection: if a later copy fails,
earlier documents may already have moved. Rerun the same call to continue.

The return value includes eligible documents already present at the destination,
so a rerun still supplies work after the source Markdown has been moved. Existing
source and destination summary JSON, partial progress, and checkpoint files stay
in their current locations and are not rewritten or copied by this helper. The
normal summary compatibility checks still apply when the destination is
processed. See the [summary guide](../summary/README.md) and the collection cells
in [the object-summary notebook](../../nbs/05.application.summary.object.ipynb).

## Tutorial: copy the corresponding original PDFs

Start with a preview:

```sh
python utils/corpus/copy_remaining_pdfs.py --dry-run
```

Then run the copy with the same inputs:

```sh
python utils/corpus/copy_remaining_pdfs.py
```

Defaults are resolved from the script's repository location, independently of
the current working directory:

| Option | Default relative to repository root |
| --- | --- |
| `--markdown-dir` | `assets/md.json.semantics.remaining` |
| `--pdf-dir` | `assets/新型机器人智能问答系统数据源-merged` |
| `--output-dir` | `assets/pdf.remaining` |
| `--dry-run` | Off; when supplied, report matches without creating or copying files. |

For other corpora, supply all three roots explicitly:

```sh
python -m utils.corpus.copy_remaining_pdfs \
  --markdown-dir assets/my-remaining-md \
  --pdf-dir assets/my-source-pdfs \
  --output-dir assets/my-remaining-pdfs \
  --dry-run
```

The converter's extra document directory is removed when matching PDFs:

| Markdown relative path | Corresponding PDF relative path |
| --- | --- |
| `category/title/title.md` | `category/title.pdf` |
| `category/title.md` | `category/title.pdf` |
| `revision-a/title/title.md` | `revision-a/title.pdf` |

The PDF's relative path is retained beneath the output directory. The script
does not fall back to a PDF with the same basename in another folder. Unmatched
Markdown paths are printed and skipped; multiple Markdown paths matching one PDF
produce one copy.

The final report lists Markdown files, unique matched PDFs, copies (or files
that would be copied), identical reuses, skipped paths, and the output directory.
Both input roots must exist, the Markdown root must contain Markdown, and the
output must be separate from both inputs. Destination conflicts are checked
before copying. New PDFs are published atomically; identical copies are reused,
and differing copies cause an error. Neither input corpus is changed.

For Python callers, `copy_remaining_pdfs(markdown_dir, pdf_dir, output_dir,
dry_run=False)` returns a dictionary with `markdown_files`, `matched_pdfs`,
`copied`, `reused`, and `skipped` counts. The `copied` count is zero in dry-run
mode.

## Relocate Markdown without a matching PDF

```sh
python utils/corpus/move_remaining_office_markdown.py --dry-run
python utils/corpus/move_remaining_office_markdown.py
```

The script compares `assets/md.json.semantics.remaining` with
`assets/pdf.remaining` using the same relative-path rules as the PDF copy tool.
It moves the unmatched Markdown to `assets/office.remaining`, preserving the
folder hierarchy and copying image resources alongside it. Summary JSON and
checkpoints stay in their original locations. Use `--markdown-dir`, `--pdf-dir`,
and `--output-dir` to override the defaults.

## Validation and troubleshooting

Run focused offline checks in the project environment:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest --confcutdir=tests -q \
  tests/test_markdown_corpus.py \
  tests/test_utils_entrypoints.py -k 'markdown or corpus'
```

For a missing-directory error, check the selected roots before creating a new
corpus. For a differing destination file, inspect the two versions and choose a
separate output root if they represent different documents. For skipped PDFs,
check the exact relative path and converter nesting; changing the basename
matching rule can mix revisions.

[Back to the utility guide](../README.md)
