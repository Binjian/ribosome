"""Exercise the self-contained notebook without executing its real-data batch cell."""

import base64
import json
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile

import nbformat
import pytest
from bs4 import BeautifulSoup


NOTEBOOK = Path(__file__).resolve().parents[1] / "nbs/03.preprocessing.parsing.code.ipynb"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


@pytest.fixture
def api():
    notebook = nbformat.read(NOTEBOOK, as_version=4)
    nbformat.validate(notebook)
    namespace = {}
    # Saved execution counts and outputs do not affect the code under test.
    for index, cell in enumerate(notebook.cells):
        if cell.cell_type == "code":
            code = compile(cell.source, f"{NOTEBOOK}:cell-{index}", "exec")
            if not cell.source.lstrip().startswith("# | notest"):
                exec(code, namespace)
    return namespace


def paragraph(text, style=None, properties=""):
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    return f"<w:p><w:pPr>{style_xml}{properties}</w:pPr><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def numbered(num_id, level=None):
    level_xml = f'<w:ilvl w:val="{level}"/>' if level is not None else ""
    return f'<w:numPr>{level_xml}<w:numId w:val="{num_id}"/></w:numPr>'


def make_docx(path, body=None, numbering_extra="", styles_extra="", numbering_replace=None):
    """Write a tiny real DOCX fixture without depending on python-docx."""
    styles = f'''<w:styles xmlns:w="{W}">
      <w:style w:type="paragraph" w:styleId="HeadingOne"><w:name w:val="Custom chapter"/>
        <w:pPr><w:outlineLvl w:val="0"/>{numbered(7, 0)}</w:pPr></w:style>
      <w:style w:type="paragraph" w:styleId="HeadingTwo"><w:name w:val="Custom subsection"/>
        <w:basedOn w:val="HeadingOne"/><w:pPr><w:outlineLvl w:val="1"/>
          <w:numPr><w:ilvl w:val="1"/></w:numPr></w:pPr></w:style>
      <w:style w:type="paragraph" w:styleId="Unnumbered"><w:name w:val="标题 2"/>
        <w:pPr><w:outlineLvl w:val="1"/>{numbered(0)}</w:pPr></w:style>
      {styles_extra}</w:styles>'''
    numbering = numbering_replace or f'''<w:numbering xmlns:w="{W}">
      <w:abstractNum w:abstractNumId="10">
        <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>
          <w:pStyle w:val="HeadingOne"/><w:lvlText w:val="%1"/></w:lvl>
        <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/>
          <w:pStyle w:val="HeadingTwo"/><w:lvlText w:val="%1.%2"/></w:lvl>
      </w:abstractNum>
      <w:num w:numId="7"><w:abstractNumId w:val="10"/></w:num>
      <w:num w:numId="8"><w:abstractNumId w:val="10"/>
        <w:lvlOverride w:ilvl="0"><w:startOverride w:val="3"/></w:lvlOverride>
        <w:lvlOverride w:ilvl="1"><w:lvl w:ilvl="1"><w:start w:val="5"/>
          <w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl></w:lvlOverride>
      </w:num>{numbering_extra}</w:numbering>'''
    if body is None:
        body = (
            paragraph("介绍")
            + paragraph("安全", "HeadingOne")
            + paragraph("使用者定义", "HeadingTwo")
            + paragraph("下一章", "HeadingOne")
            + paragraph("2.1 手工编号", "HeadingTwo")
            + paragraph("说明", "Unnumbered")
        )
    package_types = '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
      <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
      <Default Extension="xml" ContentType="application/xml"/>
      <Default Extension="png" ContentType="image/png"/>
      <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
      </Types>'''
    document = f'''<w:document xmlns:w="{W}"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
      xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
      <w:body>{body}<w:sectPr/></w:body></w:document>'''
    relations = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>'
    relation = '<Relationship Id="{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{}" Target="{}"/>'
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", package_types)
        archive.writestr("_rels/.rels", relations.format(relation.format("r1", "officeDocument", "word/document.xml")))
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/numbering.xml", numbering)
        archive.writestr("word/_rels/document.xml.rels", relations.format(
            relation.format("rStyles", "styles", "styles.xml")
            + relation.format("rNums", "numbering", "numbering.xml")
            + relation.format("rImage", "image", "media/image.png")
        ))
        archive.writestr("word/media/image.png", PNG)
    return path


def heading_titles(api, path):
    numbered_docx, style_map, headings = api["prepare_numbered_docx"](path.read_bytes())
    result = api["mammoth"].convert_to_html(numbered_docx, style_map=style_map)
    soup = BeautifulSoup(result.value, "html.parser")
    assert [h.get_text() for h in soup.select("h1,h2,h3,h4,h5,h6")] == [h["title"] for h in headings]
    return [h["title"] for h in headings]


def test_inherited_heading_numbers_typed_labels_and_restarts(api, tmp_path):
    path = make_docx(tmp_path / "手册.docx")
    original = path.read_bytes()
    assert heading_titles(api, path) == ["1 安全", "1.1 使用者定义", "2 下一章", "2.1 手工编号", "说明"]
    assert path.read_bytes() == original


def test_start_and_level_overrides_and_nonheading_counter(api, tmp_path):
    body = (
        paragraph("body list", properties=numbered(8, 0))
        + paragraph("章", "HeadingOne", numbered(8, 0))
        + paragraph("节", "HeadingTwo", numbered(8, 1))
        + paragraph("另一节", "HeadingTwo", numbered(8, 1))
        + paragraph("下一章", "HeadingOne", numbered(8, 0))
        + paragraph("重启", "HeadingTwo", numbered(8, 1))
    )
    path = make_docx(tmp_path / "overrides.docx", body)
    assert heading_titles(api, path) == ["4 章", "4.5 节", "4.6 另一节", "5 下一章", "5.5 重启"]


def test_never_restart_and_style_link(api, tmp_path):
    extra = f'''<w:abstractNum w:abstractNumId="11"><w:numStyleLink w:val="NumStyle"/></w:abstractNum>
      <w:num w:numId="9"><w:abstractNumId w:val="11"/>
        <w:lvlOverride w:ilvl="1"><w:lvl w:ilvl="1"><w:start w:val="1"/>
        <w:lvlRestart w:val="0"/><w:numFmt w:val="decimal"/>
        <w:lvlText w:val="%1.%2"/></w:lvl></w:lvlOverride></w:num>'''
    styles = f'<w:style w:type="numbering" w:styleId="NumStyle"><w:pPr>{numbered(7)}</w:pPr></w:style>'
    body = "".join(paragraph(text, style, numbered(9, level)) for text, style, level in [
        ("A", "HeadingOne", 0), ("B", "HeadingTwo", 1), ("C", "HeadingOne", 0), ("D", "HeadingTwo", 1)
    ])
    path = make_docx(tmp_path / "linked.docx", body, extra, styles)
    assert heading_titles(api, path) == ["1 A", "1.1 B", "2 C", "2.2 D"]


@pytest.mark.parametrize(("number", "fmt", "expected"), [
    (27, "upperLetter", "AA"), (4, "lowerRoman", "iv"), (3, "decimalZero", "03"),
    (101, "chineseCountingThousand", "一百零一"), (12, "japaneseCounting", "十二"),
])
def test_number_formats(api, number, fmt, expected):
    assert api["_format_number"](number, fmt) == expected


IMAGE = '''<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="pixel" descr="图片"/>
  <a:graphic><a:graphicData>
  <pic:pic><pic:nvPicPr><pic:cNvPr id="1" name="pixel" descr="图片"/></pic:nvPicPr>
  <pic:blipFill><a:blip r:embed="rImage"/></pic:blipFill></pic:pic>
  </a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'''


def test_html_tables_nested_merges_images_and_sections(api, tmp_path):
    inner = f'<w:tbl><w:tr><w:tc>{paragraph("nested")}</w:tc></w:tr></w:tbl>'
    table = f'''<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>
      <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr>
        {paragraph("中文 < & >")}{IMAGE}</w:tc><w:tc>{paragraph("top")}</w:tc></w:tr>
      <w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr>{paragraph("")}</w:tc>
        <w:tc>{inner}{paragraph("inside", "Unnumbered")}</w:tc></w:tr></w:tbl>'''
    source = make_docx(tmp_path / "source.docx", paragraph("intro") + IMAGE + paragraph("A", "HeadingOne") + table)
    target = tmp_path / "out" / "source.md"
    result = api["convert_docx_to_markdown"](source, target, show_progress=False)
    assert result["sections"] == 2  # heading inside a table is not a section boundary
    markdown = target.read_text()
    assert "# 1 A" in markdown
    soup = BeautifulSoup(markdown, "html.parser")
    assert len(soup.find_all("table")) == 2
    assert soup.find("td", colspan="2", rowspan="2") is not None
    assert "中文 &lt; &amp; &gt;" in markdown
    assert len(soup.find_all("img")) == 1
    for image in soup.find_all("img"):
        assert (target.parent / image["src"]).read_bytes() == PNG
    assert len(list((target.parent / "img").glob("*"))) == 1  # content-addressed deduplication
    assert "![图片](img/" in markdown
    assert not (target.parent / ".conversion-work").exists()


@pytest.mark.parametrize("phase", ["preparation", "section", "uncommitted_fragment", "publication"])
def test_interrupt_resume_matches_uninterrupted(api, tmp_path, monkeypatch, phase):
    source = make_docx(tmp_path / "source.docx")
    baseline = tmp_path / "baseline" / "source.md"
    api["convert_docx_to_markdown"](source, baseline, show_progress=False)
    target = tmp_path / "interrupted" / "source.md"
    expected_resumed = 0
    with monkeypatch.context() as patch:
        if phase == "preparation":
            original = api["mammoth"].convert_to_html

            def interrupt(*args, **kwargs):
                original(*args, **kwargs)
                raise KeyboardInterrupt

            patch.setattr(api["mammoth"], "convert_to_html", interrupt)
        elif phase == "section":
            original = api["_atomic_json"]

            def interrupt(path, record):
                original(path, record)
                if len(record["completed"]) == 2:
                    raise KeyboardInterrupt

            patch.setitem(api, "_atomic_json", interrupt)
            expected_resumed = 2
        else:
            original = api["_atomic_bytes"]

            def interrupt(path, data):
                if phase == "publication" and Path(path) == target.resolve():
                    raise KeyboardInterrupt
                original(path, data)
                if phase == "uncommitted_fragment" and Path(path).name == "000001.md":
                    raise KeyboardInterrupt

            patch.setitem(api, "_atomic_bytes", interrupt)
            expected_resumed = 1 if phase == "uncommitted_fragment" else 6
        with pytest.raises(KeyboardInterrupt):
            api["convert_docx_to_markdown"](source, target, show_progress=False)
    assert not target.exists()
    result = api["convert_docx_to_markdown"](source, target, show_progress=False)
    assert result["resumed_sections"] == expected_resumed
    assert target.read_bytes() == baseline.read_bytes()
    assert not (target.parent / ".conversion-work").exists()


@pytest.mark.parametrize("change", ["source", "version", "fragment", "resume_false", "manifest", "prepared_html", "image", "warnings"])
def test_invalidated_checkpoint_or_fragment(api, tmp_path, monkeypatch, change):
    source = make_docx(tmp_path / "source.docx", IMAGE + paragraph("A", "HeadingOne") + paragraph("B", "HeadingTwo"))
    target = tmp_path / "out" / "source.md"
    original = api["_publish_document"]
    with monkeypatch.context() as patch:
        def interrupt(*args):
            raise KeyboardInterrupt
        patch.setitem(api, "_publish_document", interrupt)
        with pytest.raises(KeyboardInterrupt):
            api["convert_docx_to_markdown"](source, target, show_progress=False)
    work = target.parent / ".conversion-work"
    if change == "source":
        make_docx(source, paragraph("Changed", "HeadingOne"))
    elif change == "version":
        monkeypatch.setitem(api, "PIPELINE_VERSION", 99)
    elif change == "fragment":
        (work / "sections" / "000001.md").write_text("damaged")
        (work / "sections" / "000002.md").unlink()
    elif change == "manifest":
        (work / "checkpoint.json").write_text("{")
    elif change == "prepared_html":
        (work / "prepared.html").write_text("damaged")
    elif change == "image":
        next((work / "img").iterdir()).unlink()
    elif change == "warnings":
        manifest_path = work / "checkpoint.json"
        manifest = json.loads(manifest_path.read_text())
        del manifest["warnings"]
        manifest_path.write_text(json.dumps(manifest))
    result = api["convert_docx_to_markdown"](source, target, show_progress=False, resume=change != "resume_false")
    assert result["resumed_sections"] == (1 if change == "fragment" else 0)
    assert "damaged" not in target.read_text()
    assert api["_publish_document"] is original


def test_batch_unicode_duplicate_names_case_lockfiles_failure_and_overwrite(api, tmp_path):
    root = tmp_path / "source"
    make_docx(root / "中文" / "same.DOCX")
    make_docx(root / "other" / "same.docx", paragraph("plain document"))
    make_docx(root / "~$lock.docx")
    (root / "corrupt.docx").write_bytes(b"not a zip")
    output = tmp_path / "output"
    report = api["convert_code_docs"](root, output, show_progress=False)
    assert report["discovered"] == 3
    assert len(report["converted"]) == 2 and len(report["failed"]) == 1
    assert {r["target"].relative_to(output).as_posix() for r in report["converted"]} == {
        "中文/same/same.md", "other/same/same.md"
    }
    assert next(r for r in report["converted"] if r["sections"] == 1)
    second = api["convert_code_docs"](root, output, show_progress=False)
    assert len(second["skipped"]) == 2
    make_docx(root / "中文" / "same.DOCX", paragraph("New", "HeadingOne"))
    third = api["convert_code_docs"](root, output, overwrite=True, show_progress=False)
    assert len(third["converted"]) == 2
    assert "# 1 New" in (output / "中文/same/same.md").read_text()


def test_unresolved_heading_numbering_is_a_failure_and_batch_continues(api, tmp_path):
    root = tmp_path / "source"
    make_docx(root / "bad.docx", paragraph("Bad", "HeadingOne", numbered(999)))
    make_docx(root / "good.docx")
    report = api["convert_code_docs"](root, show_progress=False)
    assert len(report["converted"]) == 1 and len(report["failed"]) == 1
    assert "numbering instance: 999" in report["failed"][0]["error"]


def test_two_progress_bars_and_restored_inner_progress(api, tmp_path, monkeypatch):
    events = []

    class Bar:
        def __init__(self, **kwargs):
            self.position = kwargs["position"]
            self.total = kwargs["total"]
            self.current = 0
            events.append(("create", self.position, self.total))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append(("close", self.position, self.current))

        def reset(self, total):
            self.total = total
            events.append(("reset", self.position, total))

        def update(self, amount):
            self.current += amount
            events.append(("update", self.position, amount))

        def set_description(self, text):
            events.append(("description", self.position, text))

        def set_postfix(self, **kwargs):
            pass

        @staticmethod
        def write(text):
            pass

    monkeypatch.setitem(api, "tqdm", Bar)
    root = tmp_path / "source"
    make_docx(root / "sample.docx")
    original = api["_atomic_json"]
    with monkeypatch.context() as patch:
        def interrupt(path, record):
            original(path, record)
            if len(record["completed"]) == 2:
                raise KeyboardInterrupt
        patch.setitem(api, "_atomic_json", interrupt)
        with pytest.raises(KeyboardInterrupt):
            api["convert_code_docs"](root, show_progress=True)
    events.clear()
    api["convert_code_docs"](root, show_progress=True)
    assert ("create", 0, 1) in events
    assert ("create", 1, None) in events
    assert ("reset", 1, 6) in events
    assert ("update", 1, 2) in events
    assert ("close", 1, 6) in events
    assert any(event[0] == "description" and "1.1 使用者定义" in event[2] for event in events)


def test_output_collision_is_reported_without_writing(api, tmp_path):
    root = tmp_path / "source"
    make_docx(root / "same.docx")
    make_docx(root / "same.DOCX")
    report = api["convert_code_docs"](root, show_progress=False)
    assert len(report["failed"]) == 2
    assert not report["converted"]


def test_output_must_not_contain_the_source_root(api, tmp_path):
    root = tmp_path / "source"
    make_docx(root / "same.docx")
    with pytest.raises(ValueError, match="output_root"):
        api["convert_code_docs"](root, root, show_progress=False)


def test_single_file_cannot_overwrite_its_source(api, tmp_path):
    source = make_docx(tmp_path / "source.docx")
    original = source.read_bytes()
    with pytest.raises(ValueError, match="target must be a .md"):
        api["convert_docx_to_markdown"](source, source, overwrite=True, show_progress=False)
    assert source.read_bytes() == original


def test_inherited_subsections_follow_parent_list_instance(api, tmp_path):
    body = (
        paragraph("Chapter", "HeadingOne", numbered(8, 0))
        + paragraph("Child", "HeadingTwo")
        + paragraph("Sibling", "HeadingTwo")
        + paragraph("Next chapter", "HeadingOne", numbered(8, 0))
        + paragraph("Restarted child", "HeadingTwo")
        + paragraph("Explicit independent child", "HeadingTwo", numbered(7, 1))
    )
    path = make_docx(tmp_path / "parent-instance.docx", body)
    assert heading_titles(api, path) == [
        "3 Chapter", "3.5 Child", "3.6 Sibling", "4 Next chapter",
        "4.5 Restarted child", "1.1 Explicit independent child",
    ]


def test_large_html_tables_keep_pandoc_object_wrappers(api):
    import pypandoc

    # Include literal numbered instructions, which Pandoc otherwise parses as Markdown.
    row = '<tr><td><p>Alarm 1234</p></td><td colspan="2"><p>1. Check drive<br/>2. Reset</p></td></tr>'
    html = '<table><thead><tr><th>Code</th><th>Action</th></tr></thead><tbody>' + row * 2000 + '</tbody></table>'
    converter = api['HtmlTableMarkdownConverter'](heading_style='ATX')
    markdown = converter.convert(html)
    assert len(markdown.splitlines()) > 2000
    original = BeautifulSoup(html, 'html.parser')
    converted = BeautifulSoup(markdown, 'html.parser')
    assert [str(cell) for cell in converted.find_all(['td', 'th'])] == [
        str(cell) for cell in original.find_all(['td', 'th'])]
    result = json.loads(pypandoc.convert_text(
        '::: {data-object="1"}\n\n' + markdown + '\n\n:::\n',
        'json', 'markdown+fenced_divs'))
    assert len(result['blocks']) == 1
    assert result['blocks'][0]['t'] == 'Div'
    assert result['blocks'][0]['c'][0][2] == [['data-object', '1']]


def test_table_formatting_preserves_inline_and_preformatted_content(api):
    html = '<table><tr><td rowspan="2"><p>a<strong>b</strong>c</p><pre> x\n  y</pre>' \
           '<table><tr><td><img src="img/a.png"/>nested</td></tr></table></td></tr></table>'
    soup = BeautifulSoup(html, 'html.parser')
    before = str(soup)
    converted = api['HtmlTableMarkdownConverter']().convert_table(soup.table, '', set())
    output = BeautifulSoup(converted, 'html.parser')
    assert str(soup) == before
    assert str(output.p) == str(soup.p)
    assert str(output.pre) == str(soup.pre)
    assert str(output.img) == str(soup.img)
    assert output.td['rowspan'] == '2'
    assert len(output.find_all('table')) == 2


def test_literal_numbered_instruction_does_not_swallow_pandoc_div(api):
    import pypandoc

    html = '<table><tr><td><p>1. Check drive<br/>2. Clear fault</p></td></tr></table>'
    markdown = api['HtmlTableMarkdownConverter']().convert(html)
    assert '1&#46; Check drive' in markdown
    assert BeautifulSoup(markdown, 'html.parser').p.get_text() == '1. Check drive2. Clear fault'
    tree = json.loads(pypandoc.convert_text(
        '::: {data-object="1"}\n\n' + markdown + '\n\n:::\n',
        'json', 'markdown+fenced_divs'))
    assert len(tree['blocks']) == 1 and tree['blocks'][0]['t'] == 'Div'
