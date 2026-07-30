import asyncio
import base64
import inspect
import sys
from pathlib import Path

from ribosome.preprocessing.parsing.ms_office.markitdown import utils, win


def test_subprocess_runner_falls_back_when_event_loop_transport_is_unsupported(
    monkeypatch,
):
    async def unsupported_subprocess_transport(*_args, **_kwargs):
        raise NotImplementedError

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        unsupported_subprocess_transport,
    )

    stdout, stderr = asyncio.run(
        utils._run_subprocess(
            [sys.executable, "-c", "print('selector-compatible')"]
        )
    )

    assert stdout.strip() == b"selector-compatible"
    assert stderr == b""


def test_convert_office_to_md_sanitizes_output_path_components(tmp_path, monkeypatch):
    office_root = tmp_path / "office"
    office_root.mkdir()
    source = office_root / "sample file .docx"
    source.write_bytes(b"fake office content")

    async def fake_run_markitdown(source_path: Path, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("# ok", encoding="utf-8")

    monkeypatch.setattr(utils, "_run_markitdown", fake_run_markitdown)

    report = asyncio.run(
        utils.convert_office_to_md(
            office_root,
            tmp_path / "out",
            overwrite=True,
            show_progress=False,
        )
    )

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
    async def fake_gif(markdown_file, _image_folder):
        gif_files.append(markdown_file)
        return -1

    async def fake_vector(markdown_file):
        vector_files.append(markdown_file)
        return -1

    monkeypatch.setattr(win, "convert_md_gif2png_win", fake_gif)
    monkeypatch.setattr(win, "extract_md_html_images_win", fake_vector)

    async def run_conversions():
        await win.convert_gif2png_from_md(root, show_progress=False)
        await win.convert_html_wmf_emf_image_from_md(root, show_progress=False)

    asyncio.run(run_conversions())

    assert gif_files == [visible.resolve()]
    assert vector_files == [visible.resolve()]


def test_office_conversion_runs_with_bounded_concurrency(tmp_path, monkeypatch):
    office_root = tmp_path / "office"
    office_root.mkdir()
    for index in range(4):
        (office_root / f"sample-{index}.docx").write_bytes(b"fake")

    active = 0
    peak = 0

    async def fake_run_markitdown(_source: Path, target: Path) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            target.write_text("# ok", encoding="utf-8")
        finally:
            active -= 1

    monkeypatch.setattr(utils, "_run_markitdown", fake_run_markitdown)
    report = asyncio.run(
        utils.convert_office_to_md(
            office_root,
            tmp_path / "out",
            overwrite=True,
            show_progress=False,
            max_concurrency=2,
        )
    )

    assert peak == 2
    assert len(report["converted"]) == 4


def test_shared_processing_apis_are_async():
    for function in (
        utils.get_office_files_root,
        utils.extract_mpo_frames,
        utils.convert_office_to_md,
        utils.extract_base64_images,
        utils.extract_base64_from_md,
        utils.process_office_files,
        win.convert_md_gif2png_win,
        win.convert_gif2png_from_md,
        win.extract_md_html_images_win,
        win.convert_html_wmf_emf_image_from_md,
    ):
        assert inspect.iscoroutinefunction(function)


def test_extract_base64_images_is_awaitable(tmp_path):
    markdown_file = tmp_path / "document.md"
    payload = base64.b64encode(b"image-bytes").decode("ascii")
    markdown_file.write_text(
        f"![robot](data:image/png;base64,{payload})",
        encoding="utf-8",
    )

    extracted = asyncio.run(utils.extract_base64_images(markdown_file))

    image_file = tmp_path / "img" / "0001_robot.png"
    assert extracted == 1
    assert image_file.read_bytes() == b"image-bytes"
    assert "img/0001_robot.png" in markdown_file.read_text(encoding="utf-8")


def test_windows_single_file_image_converters_are_awaitable(tmp_path, monkeypatch):
    gif_file = tmp_path / "robot.gif"
    vector_file = tmp_path / "diagram.wmf"
    gif_file.write_bytes(b"gif")
    vector_file.write_bytes(b"wmf")
    gif_markdown = tmp_path / "gif.md"
    vector_markdown = tmp_path / "vector.md"
    gif_markdown.write_text("![robot](robot.gif)", encoding="utf-8")
    vector_markdown.write_text(
        '<img src="diagram.wmf">',
        encoding="utf-8",
    )
    converted_paths = []

    async def fake_magick(source: Path, target: Path) -> None:
        converted_paths.append((source, target))
        target.write_bytes(b"converted")

    monkeypatch.setattr(win, "_magick_convert", fake_magick)

    async def run_conversions():
        gif_count = await win.convert_md_gif2png_win(gif_markdown)
        vector_count = await win.extract_md_html_images_win(vector_markdown)
        return gif_count, vector_count

    assert asyncio.run(run_conversions()) == (1, 1)
    assert "robot.png" in gif_markdown.read_text(encoding="utf-8")
    assert "diagram.png" in vector_markdown.read_text(encoding="utf-8")
    assert [target.suffix for _, target in converted_paths] == [
        ".png",
        ".svg",
        ".png",
    ]


def test_windows_vector_converter_handles_markdown_html_and_svg(
    tmp_path, monkeypatch
):
    image_folder = tmp_path / "img"
    image_folder.mkdir()
    wmf_file = image_folder / "drawing.wmf"
    emf_file = image_folder / "schematic.emf"
    svg_file = image_folder / "logo.svg"
    for image_file in (wmf_file, emf_file, svg_file):
        image_file.write_bytes(b"vector")

    markdown_file = tmp_path / "vectors.md"
    markdown_file.write_text(
        "\n".join(
            (
                "![](img/drawing.wmf)",
                "![schematic](img/schematic.emf)",
                '<img class="figure" src="img/logo.svg">',
            )
        ),
        encoding="utf-8",
    )
    converted_paths = []

    async def fake_magick(source: Path, target: Path) -> None:
        converted_paths.append((source, target))
        target.write_bytes(b"converted")

    monkeypatch.setattr(win, "_magick_convert", fake_magick)

    converted = asyncio.run(win.extract_md_html_images_win(markdown_file))

    assert converted == 3
    rewritten = markdown_file.read_text(encoding="utf-8")
    assert "img/drawing.png" in rewritten
    assert "img/schematic.png" in rewritten
    assert 'src="img/logo.png"' in rewritten
    assert converted_paths == [
        (wmf_file, image_folder / "drawing.svg"),
        (wmf_file, image_folder / "drawing.png"),
        (emf_file, image_folder / "schematic.svg"),
        (emf_file, image_folder / "schematic.png"),
        (svg_file, image_folder / "logo.png"),
    ]
