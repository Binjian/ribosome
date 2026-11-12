"""Filesystem coverage for optional Markdown difference collection; no model calls."""

from pathlib import Path

import pytest

from utils import markdown_corpus as corpus


def write(root, relative, content='manual'):
    file = root / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding='utf-8')
    return file


def test_difference_accepts_one_or_multiple_roots_and_compares_relative_paths(tmp_path):
    source, first, second = [tmp_path / name for name in ['source', 'first', 'second']]
    a = write(source, 'nested/manual.MD')
    b = write(source, 'other/manual.MD')
    c = write(source, 'version5/guide.md')
    write(first, 'nested/manual.MD', 'different contents still exclude the same relative path')
    write(second, 'version5/guide.md')
    write(source, 'ignored.json')
    assert corpus.markdown_difference(source, str(first)) == sorted([b, c])
    assert corpus.markdown_difference(source, iter([first, second])) == [b]
    assert a.exists() and b.exists() and c.exists()


def test_move_preserves_hierarchy_images_and_existing_summaries_and_checkpoints(tmp_path):
    source, comparison = tmp_path / 'source', tmp_path / 'comparison'
    destination = tmp_path / 'assets' / 'remaining'
    retained = write(source, 'keep/manual.MD', '![logo](img/logo.png)')
    added = write(source, 'other/manual.md')
    image = write(source, 'keep/img/logo.png', 'image bytes')
    excluded = write(source, 'covered/manual.md')
    write(comparison, 'covered/manual.md')
    existing = write(destination, 'keep/manual.MD', retained.read_text())
    artifacts = [
        write(source, 'keep/manual_semantics.json', 'source prose and object summaries'),
        write(source, '.object-summary-checkpoint.json', 'source checkpoint'),
        write(destination, 'keep/manual_semantics.json', 'saved destination summaries'),
        write(destination, 'keep/manual_summary-progress.json', 'saved partial progress'),
        write(destination, '.object-summary-checkpoint.json', 'destination checkpoint'),
    ]
    saved = {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in [existing, *artifacts]}
    expected = sorted([destination / 'keep/manual.MD', destination / 'other/manual.md'])
    assert corpus.move_markdown_difference(source, comparison, destination) == expected
    assert not retained.exists() and not added.exists() and excluded.exists()
    assert (destination / 'keep/img/logo.png').read_bytes() == image.read_bytes()
    assert image.exists() and not (destination / 'covered/manual.md').exists()
    assert {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in saved} == saved
    assert corpus.move_markdown_difference(source, comparison, destination) == expected
    assert {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in saved} == saved


def test_rerun_reuses_destination_after_all_source_markdown_was_moved(tmp_path):
    source, comparison = tmp_path / 'source', tmp_path / 'comparison'
    comparison.mkdir()
    file = write(source, 'manual.md')
    destination = tmp_path / 'assets' / 'remaining'
    expected = [destination / file.name]
    assert corpus.move_markdown_difference(source, comparison, destination) == expected
    assert list(source.rglob('*.md')) == []
    assert corpus.move_markdown_difference(source, comparison, destination) == expected


@pytest.mark.parametrize('conflict', ['b.md', 'img/logo.png'])
def test_all_destination_conflicts_are_checked_before_any_move(tmp_path, conflict):
    source, comparison = tmp_path / 'source', tmp_path / 'comparison'
    comparison.mkdir()
    first = write(source, 'a.md')
    second = write(source, 'b.md')
    write(source, 'img/logo.png', 'original image')
    destination = tmp_path / 'assets' / 'remaining'
    conflicting = write(destination, conflict, 'different destination contents')
    checkpoint = write(destination, '.object-summary-checkpoint.json', 'completed checkpoint')
    before = {file: file.read_bytes() for file in [conflicting, checkpoint]}
    with pytest.raises(FileExistsError, match='no files moved'):
        corpus.move_markdown_difference(source, comparison, destination)
    assert first.exists() and second.exists()
    assert {file: file.read_bytes() for file in destination.rglob('*') if file.is_file()} == before


def test_failed_copy_keeps_source_and_published_outputs_and_can_retry(tmp_path, monkeypatch):
    source, comparison = tmp_path / 'source', tmp_path / 'comparison'
    comparison.mkdir()
    file = write(source, 'manual.md')
    destination = tmp_path / 'assets' / 'remaining'
    checkpoint = write(destination, '.object-summary-checkpoint.json', 'completed checkpoint')
    output = write(destination, 'manual_semantics.json', 'published summary')
    before = {file: file.read_bytes() for file in [checkpoint, output]}
    copy = corpus.copy2

    def fail_copy(source, temporary):
        Path(temporary).write_bytes(b'interrupted temporary copy')
        raise OSError('copy failed')

    monkeypatch.setattr(corpus, 'copy2', fail_copy)
    with pytest.raises(OSError, match='copy failed'):
        corpus.move_markdown_difference(source, comparison, destination)
    assert file.exists()
    assert {p: p.read_bytes() for p in destination.rglob('*') if p.is_file()} == before
    monkeypatch.setattr(corpus, 'copy2', copy)
    assert corpus.move_markdown_difference(source, comparison, destination) == [destination / file.name]
    assert {file: file.read_bytes() for file in before} == before


@pytest.mark.parametrize('missing', ['source', 'comparison'])
def test_missing_input_fails_before_creating_destination(tmp_path, missing):
    source, comparison = tmp_path / 'source', tmp_path / 'comparison'
    if missing != 'source':
        write(source, 'manual.md')
    if missing != 'comparison':
        comparison.mkdir()
    destination = tmp_path / 'assets' / 'remaining'
    with pytest.raises(FileNotFoundError, match='does not exist'):
        corpus.move_markdown_difference(source, comparison, destination)
    assert not destination.exists()


@pytest.mark.parametrize('relative', ['source', 'source/nested', 'comparison/nested', '.'])
def test_destination_cannot_overlap_an_input(tmp_path, relative):
    source, comparison = tmp_path / 'source', tmp_path / 'comparison'
    file = write(source, 'manual.md')
    comparison.mkdir()
    with pytest.raises(ValueError, match='separate'):
        corpus.move_markdown_difference(source, comparison, tmp_path / relative)
    assert file.exists()
