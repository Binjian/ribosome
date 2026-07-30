from pathlib import Path

from ribosome.preprocessing.parsing.ms_office.markitdown import utils


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
