import asyncio
import copy
import json
import os
from types import SimpleNamespace

import pytest
from PIL import Image
from ribosome.preprocessing.ocr import baidu_unlimited, glm
from ribosome.preprocessing.ocr.utils import _source_signature


class _FakeAsyncOpenAIClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    async def _create(self, **_kwargs):
        await asyncio.sleep(0.001)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            id="response-1",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=response),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        )


class _FakeLayoutDetector:
    def process(self, _images, save_visualization=False):
        assert not save_visualization
        return [
            [
                {
                    "index": 0,
                    "label": "text",
                    "score": 0.9,
                    "bbox_2d": [0, 0, 1000, 1000],
                    "task_type": "text",
                }
            ]
        ], None


def _provider_case(name: str):
    if name == "baidu":
        settings = baidu_unlimited._layout_settings(
            service_base_url="http://127.0.0.1:7870/v1",
            model="baidu/Unlimited-OCR",
            prompt="<image>document parsing.",
            dpi=None,
            layout_model="PaddlePaddle/PP-DocLayoutV3_safetensors",
            layout_threshold=0.3,
            embed_page_image=False,
            max_tokens=8192,
            ngram_size=35,
            ngram_window=128,
            max_data_url_bytes=64 * 1024 * 1024,
            clean_output=True,
        )
        return (
            baidu_unlimited._prepare_layout_workspace,
            baidu_unlimited._write_layout_checkpoint,
            settings,
        )
    settings = glm._layout_settings(
        provider="ollama",
        model="glm-ocr",
        prompt=None,
        dpi=None,
        layout_model="PaddlePaddle/PP-DocLayoutV3_safetensors",
        layout_threshold=0.3,
        embed_page_image=False,
        max_output_tokens=4096,
    )
    return glm._prepare_layout_workspace, glm._write_layout_checkpoint, settings


@pytest.mark.parametrize("provider", ("baidu", "glm"))
def test_provider_checkpoint_source_signature_policy(tmp_path, provider):
    prepare, write_checkpoint, settings = _provider_case(provider)
    source = tmp_path / "source.bin"
    source.write_bytes(b"same-source")
    target = tmp_path / f"{provider}.md"

    _, _, layout = prepare(
        source,
        target,
        pages_total=1,
        settings=settings,
        resume_partial=True,
        overwrite=False,
    )
    signature = _source_signature(source)
    assert layout["source_signature"] == signature
    assert len(signature["sha256"]) == 64
    checkpoint = tmp_path / f".{provider}.layout-work" / "checkpoint.json"

    legacy = copy.deepcopy(layout)
    legacy["source_signature"].pop("sha256")
    write_checkpoint(target, legacy)
    _, _, upgraded = prepare(
        source,
        target,
        pages_total=1,
        settings=settings,
        resume_partial=True,
        overwrite=False,
    )
    assert upgraded["source_signature"] == signature
    assert json.loads(checkpoint.read_text(encoding="utf-8"))[
        "source_signature"
    ] == signature

    relocated = tmp_path / "relocated.bin"
    relocated.write_bytes(source.read_bytes())
    os.utime(
        relocated,
        ns=(signature["mtime_ns"], signature["mtime_ns"]),
    )
    write_checkpoint(target, legacy)
    with pytest.raises(ValueError, match="incompatible"):
        prepare(
            relocated,
            target,
            pages_total=1,
            settings=settings,
            resume_partial=True,
            overwrite=False,
        )

    write_checkpoint(target, layout)
    _, _, moved = prepare(
        relocated,
        target,
        pages_total=1,
        settings=settings,
        resume_partial=True,
        overwrite=False,
    )
    relocated_signature = _source_signature(relocated)
    assert moved["source_signature"] == relocated_signature
    assert json.loads(checkpoint.read_text(encoding="utf-8"))[
        "source_signature"
    ] == relocated_signature

    relocated.write_bytes(b"same-sourcf")
    os.utime(
        relocated,
        ns=(signature["mtime_ns"], signature["mtime_ns"]),
    )
    with pytest.raises(ValueError, match="incompatible"):
        prepare(
            relocated,
            target,
            pages_total=1,
            settings=settings,
            resume_partial=True,
            overwrite=False,
        )


def test_baidu_provider_replacement_preserves_repair_history(tmp_path):
    async def run():
        source = tmp_path / "source.png"
        target = tmp_path / "source.md"
        Image.new("RGB", (240, 120), "white").save(source)
        region = {
            "index": 1,
            "label": "text",
            "score": 0.9,
            "bbox": [0, 0, 240, 120],
            "task_type": "text",
            "status": "failed",
            "prompt": baidu_unlimited._DEFAULT_PROMPT,
            "max_tokens": baidu_unlimited._DEFAULT_LAYOUT_TEXT_MAX_TOKENS,
            "asset": None,
            "raw_content": "failed raw response",
            "content": "",
            "error": "RuntimeError: provider failed",
            "recovery": None,
            "finish_reason": None,
        }
        history = [
            {
                "action": "retry_region",
                "status": region["status"],
                "raw_content": region["raw_content"],
                "content": region["content"],
                "error": region["error"],
                "recovery": region["recovery"],
                "finish_reason": region.get("finish_reason"),
                "reasons": ["provider failed"],
            }
        ]
        region["repair_history"] = history
        page_record = {
            "page_number": 1,
            "width": 240,
            "height": 120,
            "render_pixels": 240 * 120,
            "requested_dpi": None,
            "effective_dpi": None,
            "status": "partial",
            "error": None,
            "regions": [region],
        }
        stage_assets = tmp_path / "stage" / "source.assets"
        stage_assets.mkdir(parents=True)
        _, repaired_page = await baidu_unlimited._layout_page_async(
            baidu_unlimited._load_image_pixmap(source),
            1,
            target=target,
            stage_assets=stage_assets,
            detector=_FakeLayoutDetector(),
            client=_FakeAsyncOpenAIClient(("replacement text",)),
            model=baidu_unlimited._DEFAULT_MODEL,
            prompt=baidu_unlimited._DEFAULT_PROMPT,
            embed_page_image=False,
            request_semaphore=None,
            layout_semaphore=None,
            max_tokens=baidu_unlimited._DEFAULT_MAX_TOKENS,
            ngram_size=baidu_unlimited._DEFAULT_NGRAM_SIZE,
            ngram_window=baidu_unlimited._DEFAULT_NGRAM_WINDOW,
            max_data_url_bytes=baidu_unlimited._DEFAULT_MAX_DATA_URL_BYTES,
            clean_output=True,
            existing_record=page_record,
        )
        assert repaired_page["status"] == "completed"
        repaired_region = repaired_page["regions"][0]
        assert repaired_region["content"] == "replacement text"
        assert repaired_region["repair_history"] == history

    asyncio.run(run())
