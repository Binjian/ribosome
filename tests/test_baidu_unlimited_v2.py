"""Offline v2 protocol, evidence, continuation, and durable retry coverage."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pymupdf
import pytest
from openai import AsyncOpenAI
from PIL import Image
from ribosome.preprocessing.conditioning import markdown as conditioning
from ribosome.preprocessing.ocr import baidu_unlimited as ocr
from ribosome.preprocessing.ocr.layout_bundle import (
    LayoutBundlePaths,
    LayoutBundleValidator,
    _merge_native_content,
    ingest_layout_bundle,
)


def block(label, content, bbox="[100,100,900,300]", level=""):
    return f"<BLOCK>{label}{level}{bbox}<FORK>{content}"


def pdf(path, pages=1):
    with pymupdf.open() as document:
        for _ in range(pages):
            document.new_page(width=240, height=120)
        document.save(path)


def image(path):
    Image.new("RGB", (240, 120), "white").save(path)


class Stream:
    def __init__(self, text, finish="stop", error=None):
        self.text, self.finish, self.error = text, finish, error
        self.closed = False

    async def __aiter__(self):
        # Split inside control tokens, as an actual token stream can do.
        for offset in range(0, len(self.text), 17):
            yield {
                "id": "native-response",
                "choices": [
                    {
                        "delta": {"content": self.text[offset : offset + 17]},
                        "finish_reason": None,
                    }
                ],
            }
            await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        if self.finish is not None:
            yield {"choices": [{"delta": {}, "finish_reason": self.finish}]}
        yield {
            "choices": [],
            "usage": {"prompt_tokens": 30, "completion_tokens": 50, "total_tokens": 80},
        }

    async def close(self):
        self.closed = True


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError(
                "Unexpected OCR request: cached response should have been reused"
            )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture(autouse=True)
def v2_model(monkeypatch):
    monkeypatch.setenv("UNLIMITED_OCR_MODEL", "Unlimited-OCR-2")


def test_native_document_payload_bundle_and_consumers(tmp_path, monkeypatch):
    source, target = tmp_path / "manual.pdf", tmp_path / "manual.md"
    pdf(source, 3)
    first_table = "<table><tr><th>Name</th></tr><tr><td>Alpha</td></tr></table>"
    next_table = "<table><tr><th>Name</th></tr><tr><td>Beta</td></tr></table>"
    raw = (
        "<PAGE>"
        + block("title", "Manual", level="<LEVEL1>")
        + block("cross_text", "Un")
        + block("cross_table", first_table, "[100,400,900,700]")
        + block("image", "", "[100,750,300,950]")
        + "<PAGE>"  # A blank physical page must remain page two.
        + "<PAGE>"
        + block("append_text", "limited OCR.")
        + block("append_table", next_table, "[100,400,900,700]")
    )
    stream = Stream(raw)
    client = Client(stream)
    progress = []
    result = asyncio.run(
        ocr.ocr_pdf(
            source,
            target,
            client=client,
            dpi=72,
            page_progress=lambda *args: progress.append(args),
        )
    )
    assert result.status == "processed", result.error
    assert (result.pages_total, result.pages_completed, result.regions_total) == (
        3,
        3,
        6,
    )
    assert stream.closed and len(client.calls) == 1
    call = client.calls[0]
    content = call["messages"][0]["content"]
    assert [part["type"] for part in content] == ["image_url"] * 3 + ["text"]
    assert content[-1]["text"] == "<PAGE><image>" * 3 + "Multi page parsing."
    assert call["model"] == "Unlimited-OCR-2"
    assert call["max_tokens"] == 8192 * 3 and call["stream"] is True
    extra = call["extra_body"]
    assert "vllm_xargs" not in extra
    assert extra["images_config"] == {"crop_mode": True}
    assert extra["custom_params"] == {
        "ngram_size": 35,
        "window_size": 1024,
        "page_token_id": 128827,
        "eos_token_id": 1,
        "max_page_tokens": 20000,
    }
    reference = bytes.fromhex(json.loads(extra["custom_logit_processor"])["callable"])
    assert reference == (
        b"csglang.srt.sampling.custom_logit_processor\n"
        b"DeepseekOCRNoRepeatNGramLogitProcessor\n."
    )
    layout = json.loads(result.metadata_path.read_text())
    assert layout["native"]["raw_content"] == raw
    assert layout["native"]["usage"]["total_tokens"] == 80
    assert layout["pages"][1]["regions"] == []
    assert layout["pages"][2]["page_number"] == 3
    heading = layout["pages"][0]["regions"][0]
    assert heading["score"] is None and heading["bbox"] == [24, 12, 216, 36]
    assert heading["content"] == "# Manual"
    assert len(layout["native"]["merge_groups"]) == 2
    markdown = target.read_text()
    assert "<PAGE>" not in markdown and "<BLOCK>" not in markdown
    assert "<!-- Page 2 -->" in markdown
    for page in layout["pages"]:
        for region in page["regions"]:
            if region["asset"]:
                assert (target.parent / region["asset"]).is_file()
    assert len(progress) == 3
    LayoutBundleValidator().validate(
        LayoutBundlePaths.from_layout(result.metadata_path)
    )

    ingested = ingest_layout_bundle(result.metadata_path, reconcile_native_text=False)
    text_record = next(
        record for record in ingested.records if record.exact_text == "Unlimited OCR."
    )
    assert (text_record.page_start, text_record.page_end) == (1, 3)
    assert len(text_record.region_ids) == 2
    table_record = next(
        record for record in ingested.records if "Alpha" in record.exact_text
    )
    assert table_record.exact_text.count("Name") == 1
    assert "Beta" in table_record.exact_text and len(table_record.asset_paths) == 2
    assert len(table_record.region_ids) == 2
    assert any(
        section.title == "Manual" and section.level == 1
        for section in ingested.document.sections
    )

    monkeypatch.setattr(
        conditioning,
        "validate_semantics_ready_markdown",
        lambda *_args, **_kwargs: conditioning.SemanticsEligibilityReport(
            True, {}, (), 0, 0, 0
        ),
    )
    conditioned = conditioning.condition_layout_markdown(markdown, layout)
    assert "# Manual" in conditioned.markdown
    assert conditioned.markdown.count("Unlimited OCR.") == 1
    assert conditioned.markdown.count("Alpha") == 1
    assert conditioned.markdown.count("Beta") == 1
    assert conditioned.markdown.count("Name") == 1
    assert conditioned.report.reordered_regions == 0

    # Group text must reflect a later region repair, rather than stale cached joins.
    layout["pages"][2]["regions"][0]["content"] = "limited repaired."
    repaired = conditioning.condition_layout_markdown(markdown, layout)
    assert "Unlimited repaired." in repaired.markdown
    assert "Unlimited OCR." not in repaired.markdown


def test_native_cached_response_retry_preserves_old_bundle(tmp_path, monkeypatch):
    source, target = tmp_path / "source.png", tmp_path / "source.md"
    image(source)
    target.write_text("previous markdown")
    sidecar = target.with_suffix(".layout.json")
    sidecar.write_text("previous sidecar")
    assets = tmp_path / "source.assets"
    assets.mkdir()
    (assets / "old.png").write_bytes(b"old asset")
    raw = "<PAGE>" + block("text", "Replacement.")
    client = Client(Stream(raw))
    publish = ocr._publish_layout_bundle
    with monkeypatch.context() as patch:
        patch.setattr(
            ocr,
            "_publish_layout_bundle",
            lambda *_args: (_ for _ in ()).throw(OSError("publication failed")),
        )
        result = asyncio.run(
            ocr.ocr_image(source, target, client=client, overwrite=True)
        )
    assert result.status == "failed" and "publication failed" in result.error
    assert target.read_text() == "previous markdown"
    assert sidecar.read_text() == "previous sidecar"
    assert (assets / "old.png").read_bytes() == b"old asset"
    workspace = ocr._layout_workspace_paths(target)[0]
    assert (
        json.loads((workspace / "native-response.json").read_text())["raw_content"]
        == raw
    )
    checkpoint_before = (workspace / "checkpoint.json").read_bytes()
    incompatible = asyncio.run(
        ocr.ocr_image(source, target, client=Client(), max_tokens=8193)
    )
    assert incompatible.status == "failed" and "incompatible" in incompatible.error
    assert (workspace / "checkpoint.json").read_bytes() == checkpoint_before
    assert ocr._publish_layout_bundle is publish
    retry_client = Client()
    result = asyncio.run(ocr.ocr_image(source, target, client=retry_client))
    assert result.status == "processed", result.error
    assert retry_client.calls == [] and "Replacement." in target.read_text()
    assert not workspace.exists()


@pytest.mark.parametrize("finish", [None, "length"])
def test_unfinished_stream_does_not_publish(tmp_path, finish):
    source, target = tmp_path / "source.png", tmp_path / "source.md"
    image(source)
    raw = "<PAGE>" + block("text", "Incomplete.")
    stream = Stream(raw, finish=finish)
    result = asyncio.run(ocr.ocr_image(source, target, client=Client(stream)))
    assert result.status == "failed" and "Incomplete native" in result.error
    assert not target.exists() and stream.closed
    workspace = ocr._layout_workspace_paths(target)[0]
    assert (workspace / "incomplete-response.txt").read_text() == raw
    assert not (workspace / "native-response.json").exists()


def test_cancelled_inference_retries_whole_document(tmp_path):
    source, target = tmp_path / "source.pdf", tmp_path / "source.md"
    pdf(source, 2)
    partial = Stream("<PAGE>" + block("text", "First."), error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ocr.ocr_pdf(source, target, client=Client(partial), dpi=72))
    assert not target.exists() and partial.closed
    workspace = ocr._layout_workspace_paths(target)[0]
    assert (workspace / "checkpoint.json").exists()
    assert not (workspace / "native-response.json").exists()
    client = Client(
        Stream("<PAGE>" + block("text", "First.") + "<PAGE>" + block("text", "Second."))
    )
    result = asyncio.run(ocr.ocr_pdf(source, target, client=client, dpi=72))
    assert result.status == "processed", result.error
    assert len(client.calls[0]["messages"][0]["content"]) == 3


def test_cached_response_survives_parser_failure_and_source_change(
    tmp_path, monkeypatch
):
    source, target = tmp_path / "source.png", tmp_path / "source.md"
    image(source)
    raw = "<PAGE>" + block("text", "Retained.")
    parser = ocr._native_blocks
    with monkeypatch.context() as patch:
        patch.setattr(
            ocr,
            "_native_blocks",
            lambda *_args: (_ for _ in ()).throw(ValueError("parser failure")),
        )
        result = asyncio.run(ocr.ocr_image(source, target, client=Client(Stream(raw))))
    assert result.status == "failed" and "parser failure" in result.error
    assert not target.exists()
    retry = Client()
    monkeypatch.setattr(
        ocr,
        "_LAYOUT_RESPONSE_VALIDATION_VERSION",
        ocr._LAYOUT_RESPONSE_VALIDATION_VERSION + 1,
    )
    result = asyncio.run(ocr.ocr_image(source, target, client=retry))
    assert result.status == "processed", result.error
    assert retry.calls == [] and ocr._native_blocks is parser

    # An input change must still reject a previously completed provider response.
    other_target = tmp_path / "changed.md"
    with monkeypatch.context() as patch:
        patch.setattr(
            ocr,
            "_native_blocks",
            lambda *_args: (_ for _ in ()).throw(ValueError("parser failure")),
        )
        asyncio.run(ocr.ocr_image(source, other_target, client=Client(Stream(raw))))
    workspace = ocr._layout_workspace_paths(other_target)[0]
    cached = (workspace / "native-response.json").read_bytes()
    Image.new("RGB", (240, 120), "black").save(source)
    failed = asyncio.run(ocr.ocr_image(source, other_target, client=Client()))
    assert failed.status == "failed" and "incompatible" in failed.error
    assert (workspace / "native-response.json").read_bytes() == cached


def test_native_parser_merge_markers_and_validation():
    raw = (
        "<PAGE>"
        + block("title", "Part", level="<LEVEL3>")
        + "<MERGE>"
        + block("text", "Two")
    )
    pages = ocr._native_blocks(raw, 1)
    assert pages[0][0]["heading_level"] == 3
    assert ocr._native_groups(pages) == [{"kind": "text", "members": [[1, 1], [1, 2]]}]
    assert ocr.clean_grounding(raw) == "### PartTwo"
    with pytest.raises(ValueError, match="page count mismatch"):
        ocr._native_blocks(raw, 2)
    with pytest.raises(ValueError, match="coordinates"):
        ocr._native_blocks("<PAGE>" + block("text", "Bad", "[900,100,100,200]"), 1)
    with pytest.raises(ValueError, match="without layout blocks"):
        ocr._native_blocks("<PAGE>lost text", 1)
    first = "<table><tr><th>H</th></tr><tr><td>A</td></tr></table>"
    second = "<table><tr><th>H</th></tr><tr><td>B</td></tr></table>"
    assert _merge_native_content([first, second], "table").count("<th>H</th>") == 1


def test_plain_mode_uses_v2_protocol_and_cleans_blocks(tmp_path):
    source, target = tmp_path / "source.png", tmp_path / "source.md"
    image(source)
    response = {
        "id": "single",
        "choices": [
            {
                "message": {
                    "content": "<PAGE>" + block("title", "Title", level="<LEVEL2>")
                },
                "finish_reason": "stop",
            }
        ],
    }
    client = Client(response)
    result = asyncio.run(
        ocr.ocr_image(
            source,
            target,
            client=client,
            layout_mode="plain",
            crop_mode=False,
            max_page_tokens=10000,
        )
    )
    assert result.status == "processed", result.error
    assert "## Title" in target.read_text()
    extra = client.calls[0]["extra_body"]
    assert extra["images_config"] == {"crop_mode": False}
    assert extra["custom_params"]["window_size"] == 128
    assert extra["custom_params"]["max_page_tokens"] == 10000


def test_default_folder_uses_separate_v2_outputs(tmp_path):
    source = tmp_path / "source.png"
    image(source)
    old = tmp_path / ".md_unlimited"
    old.mkdir()
    (old / "source.md").write_text("old corpus")
    client = Client(Stream("<PAGE>" + block("text", "New corpus.")))
    results = asyncio.run(ocr.ocr_folder(tmp_path, client=client, show_progress=False))
    assert [result.status for result in results] == ["processed"]
    assert (old / "source.md").read_text() == "old corpus"
    assert (tmp_path / ".md_unlimited_v2" / "source.layout.json").is_file()
    skipped = asyncio.run(
        ocr.ocr_folder(tmp_path, client=Client(), show_progress=False)
    )
    assert [result.status for result in skipped] == ["skipped"]


def test_native_publication_cancellation_restores_previous_bundle(
    tmp_path, monkeypatch
):
    source, target = tmp_path / "source.png", tmp_path / "source.md"
    image(source)
    target.write_text("previous markdown")
    sidecar = target.with_suffix(".layout.json")
    sidecar.write_text("previous sidecar")
    assets = tmp_path / "source.assets"
    assets.mkdir()
    (assets / "old.png").write_bytes(b"old asset")
    replace = type(target).replace

    def interrupted(path, destination):
        if path.name == "source.layout.json" and path.parent.name.startswith(
            ".source.layout-publish-"
        ):
            raise asyncio.CancelledError()
        return replace(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(type(target), "replace", interrupted)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                ocr.ocr_image(
                    source,
                    target,
                    overwrite=True,
                    client=Client(Stream("<PAGE>" + block("text", "New text."))),
                )
            )
    assert target.read_text() == "previous markdown"
    assert sidecar.read_text() == "previous sidecar"
    assert (assets / "old.png").read_bytes() == b"old asset"
    assert (ocr._layout_workspace_paths(target)[0] / "native-response.json").is_file()
    resumed = asyncio.run(ocr.ocr_image(source, target, client=Client()))
    assert resumed.status == "processed", resumed.error


def test_entirely_blank_document_keeps_all_pages(tmp_path):
    source, target = tmp_path / "blank.pdf", tmp_path / "blank.md"
    pdf(source, 2)
    result = asyncio.run(
        ocr.ocr_pdf(source, target, dpi=72, client=Client(Stream("<PAGE><PAGE>")))
    )
    assert result.status == "processed", result.error
    assert result.pages_completed == 2 and result.regions_total == 0
    layout = json.loads(result.metadata_path.read_text())
    assert [page["regions"] for page in layout["pages"]] == [[], []]
    assert target.read_text().count("<!-- Page ") == 2


def test_invalid_native_groups_fail_bundle_validation(tmp_path):
    source, target = tmp_path / "source.png", tmp_path / "source.md"
    image(source)
    result = asyncio.run(
        ocr.ocr_image(
            source,
            target,
            client=Client(
                Stream("<PAGE>" + block("text", "One.") + block("text", "Two."))
            ),
        )
    )
    assert result.status == "processed", result.error
    layout = json.loads(result.metadata_path.read_text())
    layout["native"]["merge_groups"] = [{"kind": "text", "members": [[1, 1], [1, 99]]}]
    result.metadata_path.write_text(json.dumps(layout))
    issues = LayoutBundleValidator().validate(
        LayoutBundlePaths.from_layout(result.metadata_path), strict=False
    )
    assert "invalid_native_merge_groups" in {issue.code for issue in issues}


def test_openai_client_serializes_v2_payload_and_reads_sse():
    captured = []
    raw = "<PAGE>" + block("text", "Actual SDK stream.")

    def handler(request):
        captured.append(json.loads(request.content))
        events = [
            {
                "id": "wire",
                "choices": [
                    {"index": 0, "delta": {"content": raw}, "finish_reason": None}
                ],
            },
            {
                "id": "wire",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            {
                "id": "wire",
                "choices": [],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                },
            },
        ]
        data = (
            "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=data
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as transport:
            async with AsyncOpenAI(
                api_key="offline-test",
                base_url="http://offline.test/v1",
                http_client=transport,
            ) as client:
                return await ocr._request_v2(
                    client,
                    model="Unlimited-OCR-2",
                    images=["data:image/png;base64,AA=="],
                    prompt="Multi page parsing.",
                    max_tokens=8192,
                    ngram_size=35,
                    ngram_window=None,
                    stream=True,
                )

    content, metadata = asyncio.run(run())
    assert content == raw and metadata["usage"]["total_tokens"] == 5
    assert captured[0]["custom_params"]["page_token_id"] == 128827
    assert (
        captured[0]["messages"][0]["content"][-1]["text"]
        == "<PAGE><image>Multi page parsing."
    )
