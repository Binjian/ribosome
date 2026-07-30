from pathlib import Path

from ribosome.preprocessing.parsing.ms_office.markitdown import utils, win


def test_convert_office_to_md_sanitizes_output_path_components(tmp_path, monkeypatch):
    office_root = tmp_path / "office"
    office_root.mkdir()
    source = office_root / "sample file .docx"
    source.write_bytes(b"fake office content")

    def fake_run_markitdown(source_path: Path, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("# ok", encoding="utf-8")

    monkeypatch.setattr(utils, "_run_markitdown", fake_run_markitdown)

    report = utils.convert_office_to_md(office_root, tmp_path / "out", overwrite=True, show_progress=False)

    assert len(report["converted"]) == 1
    markdown_path = report["converted"][0]
    assert markdown_path.exists()
    assert not any(part.endswith((" ", ".")) for part in markdown_path.parts)
    assert not str(markdown_path).endswith(" .md")


def test_windows_image_conversion_ignores_dot_prefixed_folders(
    tmp_path, monkeypatch
):
    root = tmp_path / "markdown"
    visible = root / "docs" / "visible.md"
    hidden = root / ".cache" / "hidden.md"
    nested_hidden = root / "docs" / ".assets" / "hidden.md"
    for markdown_file in (visible, hidden, nested_hidden):
        markdown_file.parent.mkdir(parents=True, exist_ok=True)
        markdown_file.write_text("# test", encoding="utf-8")

    gif_files = []
    vector_files = []
    monkeypatch.setattr(
        win,
        "convert_md_gif2png_win",
        lambda markdown_file, _image_folder: gif_files.append(markdown_file) or -1,
    )
    monkeypatch.setattr(
        win,
        "extract_md_html_images_win",
        lambda markdown_file: vector_files.append(markdown_file) or -1,
    )

    win.convert_gif2png_from_md(root, show_progress=False)
    win.convert_html_wmf_emf_image_from_md(root, show_progress=False)

    assert gif_files == [visible.resolve()]
    assert vector_files == [visible.resolve()]
