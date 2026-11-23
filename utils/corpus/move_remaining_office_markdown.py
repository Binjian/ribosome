#!/usr/bin/env python3
"""Move Markdown without a matching PDF into the remaining Office corpus."""

import argparse
import sys
from pathlib import Path

if __package__:
    from .copy_remaining_pdfs import (
        PROJECT_ROOT,
        _corresponding_pdf_path,
        _pdfs_by_relative_path,
    )
    from .markdown_corpus import (
        _markdown_paths,
        _move_markdown_files,
        _validate_destination,
    )
else:
    from copy_remaining_pdfs import (
        PROJECT_ROOT,
        _corresponding_pdf_path,
        _pdfs_by_relative_path,
    )
    from markdown_corpus import (
        _markdown_paths,
        _move_markdown_files,
        _validate_destination,
    )


def move_remaining_office_markdown(markdown_dir, pdf_dir, output_dir, *, dry_run=False):
    """Return the relative paths of Markdown moved, or selected in a dry run.

    Match PDFs by relative path, allowing the extra document directory used by
    the Markdown converter. Preserve Markdown paths and copy image resources.
    """
    source, markdown_files = _markdown_paths(markdown_dir)
    pdf_dir = Path(pdf_dir).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    if not pdf_dir.is_dir():
        raise NotADirectoryError(f"PDF directory does not exist: {pdf_dir}")
    _validate_destination(destination, source, pdf_dir)

    pdfs = _pdfs_by_relative_path(pdf_dir)
    unmatched = [path for path in markdown_files
                 if _corresponding_pdf_path(path.relative_to(source)) not in pdfs]
    if unmatched:
        _move_markdown_files(source, destination, unmatched, dry_run=dry_run)
    return [path.relative_to(source) for path in unmatched]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--markdown-dir", type=Path, default=PROJECT_ROOT / "assets/md.json.semantics.remaining",
    )
    parser.add_argument("--pdf-dir", type=Path, default=PROJECT_ROOT / "assets/pdf.remaining")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "assets/office.remaining")
    parser.add_argument("--dry-run", action="store_true", help="List candidates without moving files")
    args = parser.parse_args()

    try:
        paths = move_remaining_office_markdown(
            args.markdown_dir, args.pdf_dir, args.output_dir, dry_run=args.dry_run,
        )
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    action = "Would move" if args.dry_run else "Moved"
    for path in paths:
        print(f"{action}: {path}")
    print(f"{action} {len(paths)} Markdown files to {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
