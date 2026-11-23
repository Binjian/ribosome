"""Prepare a Markdown corpus by subtracting other corpora's relative paths."""

import filecmp
import os
import tempfile
from pathlib import Path
from shutil import copy2

_IMAGE_SUFFIXES = {
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.tif', '.tiff',
    '.ico', '.avif', '.heic', '.heif', '.svg', '.emf', '.wmf',
}


def _markdown_paths(directory):
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f'Markdown directory does not exist: {root}')
    return root, sorted(path for path in root.rglob('*')
                        if path.is_file() and path.suffix.lower() == '.md')


def _comparison_roots(comparison_dirs):
    if isinstance(comparison_dirs, (str, os.PathLike)):
        comparison_dirs = [comparison_dirs]
    return [_markdown_paths(directory) for directory in comparison_dirs]


def markdown_difference(source_dir, comparison_dirs):
    """Return sorted source Markdown absent by relative path from comparison roots.

    ``comparison_dirs`` accepts one directory or an iterable of directories.
    Empty directories are valid; missing directories raise before any work.
    Basenames in different folders remain distinct. Both .md and .MD are found.
    """
    source, files = _markdown_paths(source_dir)
    excluded = {path.relative_to(root)
                for root, paths in _comparison_roots(comparison_dirs) for path in paths}
    return [path for path in files if path.relative_to(source) not in excluded]


def _check_destination(source, destination):
    if destination.is_symlink() or (
        destination.exists() and (not destination.is_file()
                                  or not filecmp.cmp(source, destination, shallow=False))
    ):
        raise FileExistsError(f'Destination differs from source; no files moved: {destination}')
    for parent in destination.parents:
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise NotADirectoryError(f'Destination parent is not a regular directory: {parent}')


def _copy_missing(source, destination):
    if destination.exists():
        return
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


def _validate_destination(destination, *input_roots):
    for root in input_roots:
        if root == destination or root in destination.parents or destination in root.parents:
            raise ValueError('Destination must be separate from source and comparison directories')


def _move_markdown_files(source, destination, files, *, dry_run=False):
    resources = sorted({path for folder in {file.parent for file in files}
                        for path in folder.rglob('*')
                        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES})
    for path in [*resources, *files]:
        _check_destination(path, destination / path.relative_to(source))
    if dry_run:
        return

    destination.mkdir(parents=True, exist_ok=True)
    for path in resources:
        _copy_missing(path, destination / path.relative_to(source))
    for path in files:
        _copy_missing(path, destination / path.relative_to(source))
        path.unlink()


def move_markdown_difference(source_dir, comparison_dirs, destination_dir):
    """Move source-minus-comparison Markdown into a separate destination corpus.

    Pass a destination under the project's assets directory. Preserve relative
    paths and copy image resources from each selected document's folder before
    moving Markdown. Source images stay available to other documents. Existing
    identical destination files are reused; all conflicts are checked before
    copying or moving anything. Publication uses temporary files and atomic
    replacement, then removes the source Markdown only after its copy succeeds.

    Return eligible destination Markdown, including previously moved documents,
    so reruns still process/resume the collected corpus after the source empties.
    Existing source and destination summaries, partial work, and checkpoints are
    untouched. No summary checkpoint compatibility checks are bypassed.
    """
    source = Path(source_dir).expanduser().resolve()
    destination = Path(destination_dir).expanduser().resolve()
    comparisons = _comparison_roots(comparison_dirs)
    # Keep a materialized comparison list so generator arguments work on reruns.
    comparison_roots = [root for root, _ in comparisons]
    _validate_destination(destination, source, *comparison_roots)
    files = markdown_difference(source, comparison_roots)
    _move_markdown_files(source, destination, files)
    return markdown_difference(destination, comparison_roots)
