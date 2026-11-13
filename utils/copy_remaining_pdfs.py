#!/usr/bin/env python3
"""Copy source PDFs corresponding to the remaining Markdown corpus.

Run from any working directory:
    python utils/copy_remaining_pdfs.py [--dry-run]

Match by relative path, removing the Markdown converter's extra document
directory: category/title/title.md corresponds to category/title.pdf. Also
accept category/title.md. Preserve source PDF paths beneath the output root.
Markdown with no PDF at the corresponding path is reported and skipped; a
same-named PDF in a different folder may belong to another document version.
"""

import argparse
import filecmp
import os
from pathlib import Path
from shutil import copy2
import sys
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def copy_remaining_pdfs(markdown_dir, pdf_dir, output_dir, *, dry_run=False):
    """Copy matching PDFs, reuse identical copies, and reject existing conflicts.

    All matches and destination conflicts are checked before copying. Each new
    PDF is published atomically. Sources and corpus checkpoints stay untouched.
    Return counts for Markdown files, matched PDFs, copies, reuses, and skips.
    """
    markdown_dir, pdf_dir, output_dir = (
        Path(path).expanduser().resolve()
        for path in (markdown_dir, pdf_dir, output_dir)
    )
    for root in (markdown_dir, pdf_dir):
        if not root.is_dir():
            raise NotADirectoryError(f"Input directory does not exist: {root}")
        if root == output_dir or root in output_dir.parents or output_dir in root.parents:
            raise ValueError("Output directory must be separate from both input directories")
    if output_dir.exists() and not output_dir.is_dir():
        raise NotADirectoryError(f"Output path is not a directory: {output_dir}")

    markdown_files = sorted(
        path for path in markdown_dir.rglob("*")
        if path.is_file() and path.suffix.lower() == ".md"
    )
    if not markdown_files:
        raise ValueError(f"No Markdown files found: {markdown_dir}")
    pdfs = {}
    for path in sorted(pdf_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".pdf":
            key = path.relative_to(pdf_dir).with_suffix(".pdf")
            if key in pdfs:
                raise ValueError(f"Multiple PDFs at the same relative path: {key}")
            pdfs[key] = path

    matches = {}
    skipped = []
    for markdown in markdown_files:
        relative = markdown.relative_to(markdown_dir)
        parent = relative.parent
        if parent.name == markdown.stem:
            parent = parent.parent
        source = pdfs.get(parent / (markdown.stem + ".pdf"))
        if source is None:
            skipped.append(relative)
        else:
            matches[source] = output_dir / source.relative_to(pdf_dir)

    pending = []
    reused = 0
    for source, destination in matches.items():
        if destination.exists():
            if not destination.is_file() or not filecmp.cmp(source, destination, shallow=False):
                raise FileExistsError(f"Existing destination differs from source: {destination}")
            reused += 1
        else:
            for parent in destination.parents:
                if parent.exists() and not parent.is_dir():
                    raise NotADirectoryError(f"Destination parent is not a directory: {parent}")
            pending.append((source, destination))

    for relative in skipped:
        print(f"Skipped (no corresponding PDF): {relative}")
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        for source, destination in pending:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                copy2(source, temporary)
                os.replace(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    print(f"Markdown files: {len(markdown_files)}")
    print(f"Matched PDFs: {len(matches)}")
    print(f"{'Would copy' if dry_run else 'Copied'}: {len(pending)}")
    print(f"Already identical: {reused}")
    print(f"Skipped (no corresponding PDF): {len(skipped)}")
    print(f"Output directory: {output_dir}")
    return {
        "markdown_files": len(markdown_files),
        "matched_pdfs": len(matches),
        "copied": 0 if dry_run else len(pending),
        "reused": reused,
        "skipped": len(skipped),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--markdown-dir", type=Path,
        default=PROJECT_ROOT / "assets/md.json.semantics.remaining",
    )
    parser.add_argument(
        "--pdf-dir", type=Path,
        default=PROJECT_ROOT / "assets/新型机器人智能问答系统数据源-merged",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "assets/pdf.remaining",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report matches without copying")
    args = parser.parse_args()
    try:
        copy_remaining_pdfs(args.markdown_dir, args.pdf_dir, args.output_dir, dry_run=args.dry_run)
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
