# Upgrade the Baidu OCR notebook to v2

## Summary

Adopt native multi-page parsing as the default, retain PP-DocLayout as an option,
and write new results to `.md_unlimited_v2`. Preserve the existing corpus and
summary checkpoints.

Follow Baidu's [v2 serving recipe](https://github.com/baidu/Unlimited-OCR/blob/main/README.md)
and [block-format conversion rules](https://github.com/baidu/Unlimited-OCR/blob/main/utils/README.md).

## Notebook and API changes

- Update the canonical notebook, preserving existing edits, cell IDs, and metadata;
  regenerate its ignored Python export through nbdev.
- Add `layout_mode="native"` and make it the default for `ocr_pdf`, `ocr_image`, and
  `ocr_folder`. Retain `plain` and `pp-doclayout`, updating their individual requests
  to the v2 protocol.
- Default to the documented served alias `Unlimited-OCR-2`, retain endpoint/model
  overrides, and change the output-directory default to `.md_unlimited_v2`.
- Send PDF pages in one request, with ordered images followed by automatically
  constructed `<PAGE><image>` markers and task text.
- Replace `vllm_xargs` with SGLang's image configuration, custom processor, and
  decoding parameters. Reference the server's processor using its compatible
  serialized class reference, without installing SGLang on the client.
- Add `crop_mode=True` and `max_page_tokens=20000`. Keep `max_tokens=8192` as the
  per-page budget; multiply it by page count for native PDF requests. Make an
  omitted n-gram window resolve to 128 for one image and 1024 for multiple pages.
- Stream native responses, preserve usage and finish information, and raise the
  default request timeout to 6000 seconds. Keep request concurrency limits.
- Update configuration instructions to address the existing `.env` override
  selecting v1 and document the required remote SGLang deployment.

## Native output and downstream compatibility

- Parse page/block markers, heading levels, normalized coordinates, and merge
  indicators. Preserve blank pages and reject page-count mismatches or truncated
  responses.
- Produce the existing schema-v2 `.layout.json`, marker-delimited Markdown, and
  `.assets/` bundle. Keep physical regions page-local, preserve model reading order,
  convert coordinates to rendered-page pixels, and generate real crop assets.
- Retain original block fragments and raw model output. Add native provenance
  containing labels, normalized boxes, heading levels, and ordered merge-group
  member references.
- Make layout ingestion and Markdown conditioning consume explicit native merge
  groups once. Preserve all member citations/assets, concatenate text continuations,
  and deduplicate repeated table headers. Derive merged content from current
  fragments so later repairs remain effective.
- Honor native reading order and heading levels before existing geometric
  heuristics. Keep legacy bundle behavior unchanged.

## Checkpoints and publication

- Keep `OVERWRITE=False` and partial-resume support enabled.
- Cache each complete native response atomically before local conversion. Resume
  parsing, cropping, or publication from that cache without another OCR request.
- Preserve source-signature and model/settings compatibility checks. Parser changes
  alone must not invalidate cached provider responses.
- Preserve incomplete response diagnostics, but retry interrupted inference as a
  whole-document request; do not combine independent page retries into a native
  document.
- Reuse rollback-safe bundle publication. Failures and cancellation must preserve
  previous published outputs and usable checkpoints.
- Leave existing OCR outputs and object-summary checkpoints untouched.

## Validation and assumptions

- Test exact payloads, lightweight processor serialization, streaming chunks, token
  budgets, errors, and truncation.
- Cover blank pages, heading levels, coordinate conversion, crops, text/table
  continuations, and duplicate-free conditioning and retrieval.
- Verify strict bundle validation, checkpoint reuse, incompatible settings,
  cancellation, and publication rollback.
- Run the notebook's offline tests and focused OCR, layout, conditioning, and
  retrieval regressions.
- Remote deployment is outside this notebook update. The configured endpoint
  refused connections during planning; perform a small opt-in smoke test when a v2
  service is available, without launching corpus processing.

## Tutorial: install and run the SGLang service

Verified against upstream documentation and Baidu's wheel metadata on
2026-11-13. These instructions prepare the **GPU server** first, then connect the
**ribosome notebook machine**. They do not require the two machines to use the
same Python environment.

### Automated installer

The standalone [host installer](../utils/deploy_unlimited_ocr_v2.py) implements the
setup below, including a private uv bootstrap when needed, a Python 3.10 venv,
checksum validation of the pinned SGLang wheel, model download, and user systemd
deployment. It needs Python 3.10+ to run and does not import ribosome. Execute it
**on the GPU host** as the service account, without sudo:

```bash
python3 utils/deploy_unlimited_ocr_v2.py deploy --dry-run --client-host 172.27.74.16
python3 utils/deploy_unlimited_ocr_v2.py deploy \
  --gpu 0 --bind 0.0.0.0 --port 7870 \
  --client-host 172.27.74.16 --smoke-test
```

If the repository is only on the notebook machine, copy just this script; it has
no adjacent-file dependencies:

```bash
scp utils/deploy_unlimited_ocr_v2.py x@172.27.74.16:/tmp/deploy_unlimited_ocr_v2.py
ssh x@172.27.74.16 \
  'python3 /tmp/deploy_unlimited_ocr_v2.py deploy --client-host 172.27.74.16 --smoke-test'
```

The installer checks GPU/driver availability and the port before downloads. Its
default directory is `~/services/unlimited-ocr-v2`; select another directory with
`--install-dir`. `service.env` and `client.env` have mode `0600`, and the systemd
unit contains no credentials. Copy settings from `client.env` into the notebook's
existing `.env`, then restart its kernel.

The installer also creates a private `cuda/` layout from the environment's NVIDIA
CUDA 13 packages, including the compiler and linker aliases needed by SGLang's
JIT kernels. The launcher uses it for `CUDA_HOME`, `CUDA_PATH`, and compiler/library
search paths, so an older `/usr/local/cuda` does not take precedence. Explicit
`CUDA_HOME` or `CUDA_PATH` entries in `service.env` can select another toolkit.

Reruns preserve the key, settings, and saved model commit. Explicit flags override
settings; `--model-revision main` selects the then-current upstream model commit.
Stop the service before reinstalling or changing its environment. A running
managed instance holds a lock to prevent concurrent changes:

```bash
python3 utils/deploy_unlimited_ocr_v2.py stop
python3 utils/deploy_unlimited_ocr_v2.py deploy --client-host 172.27.74.16
```

Use `--no-start` to install and write the unit without starting/enabling it. For a
host without a systemd user manager, use `deploy --no-systemd`, then start it with
the script's `run` action. `--skip-gpu-check` supports preparing files on a build
host and requires `--no-start` or `--no-systemd`; it does not establish GPU
compatibility. Run `check --smoke-test` on the actual GPU host afterward.

`status`, `logs`, `start`, `restart`, and `stop` manage the user service.
`client-env --client-host HOST` prints its notebook configuration, including the
API key, when that output is explicitly needed. The installer does not run a
corpus job or change the project `.env`, GPU drivers, firewall rules, or unrelated
services. Enable user lingering separately as described in step 7 below.

The remaining sections provide the manual setup and troubleshooting reference.

### 1. Check the GPU server

Use a Linux machine with an NVIDIA GPU and enough available GPU memory for the
model and page images. On the server, check:

```bash
nvidia-smi
df -h /dev/shm
ss -ltnp 'sport = :7870'
```

The example uses server `172.27.74.16`, GPU `0`, and TCP port `7870`, matching this
notebook's default endpoint. Substitute your server's address if it differs.
Choose another GPU or port if they are already occupied. Existing
Ollama or OCR processes can consume the memory this service needs; arrange
capacity before launching it.

The pinned wheel depends on CUDA 13 packages. NVIDIA lists driver **580 or newer**
as the CUDA 13.x compatibility baseline; individual compiled kernels can require
a newer driver. Check your actual driver rather than assuming an older CUDA 12
environment will work. See [NVIDIA's driver compatibility table](https://docs.nvidia.com/deploy/cuda-compatibility/latest/minor-version-compatibility.html).

Baidu's documented reference hardware includes an A800 with 80 GB. That is not a
published minimum GPU-memory requirement. In particular, the RTX 4090 mentioned
in this repository's earlier server notes does not establish that this recipe
fits a busy 24 GB GPU. Start with the conservative settings below and verify
memory usage on your machine. [Baidu model card](https://huggingface.co/baidu/Unlimited-OCR-2#inference)

### 2. Create a separate service environment

Run this section on the GPU server. Keep this Python 3.10 environment separate
from ribosome's Python 3.12+ environment. Install `curl` and normal C/C++ build
tools through your operating system's package manager if they are absent.

If `uv` is not installed, use its [official installer](https://docs.astral.sh/uv/getting-started/installation/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

Create the service directory and environment:

```bash
mkdir -p "$HOME/services/unlimited-ocr-v2"
cd "$HOME/services/unlimited-ocr-v2"

uv python install 3.10
uv venv --python 3.10 .venv
source .venv/bin/activate
mkdir -p wheel
```

Install Baidu's customized build, rather than replacing it with an arbitrary
PyPI SGLang release. The version and Python choice follow the
[model-specific SGLang instructions](https://github.com/baidu/Unlimited-OCR#sglang).

```bash
OCR_WHEEL=sglang-0.5.20.dev13+g75b9b06b02-py3-none-any.whl
OCR_WHEEL_URL='https://huggingface.co/baidu/Unlimited-OCR-2/resolve/main/wheel/sglang-0.5.20.dev13%2Bg75b9b06b02-py3-none-any.whl'

curl --fail --location "$OCR_WHEEL_URL" --output "wheel/$OCR_WHEEL"
sha256sum "wheel/$OCR_WHEEL" > wheel/installed-wheel.sha256

uv pip install --prerelease=allow \
  "wheel/$OCR_WHEEL" 'fastokens==0.3.2' 'pymupdf==1.27.2.2'
uv pip check
uv pip freeze > installed-packages.txt
```

The [distributed wheel](https://huggingface.co/baidu/Unlimited-OCR-2/tree/main/wheel)
declares its own server dependencies, including PyTorch 2.13.0 and Transformers
5.12.1. Do not apply the separate Transformers-inference example's 4.52.1 pin
to this server environment. `--prerelease=allow` lets uv resolve the development
dependencies; `uv pip check` must succeed before continuing.

Check that this environment can use the intended GPU:

```bash
CUDA_VISIBLE_DEVICES=0 python - <<'PY'
import importlib.metadata
import torch

print("SGLang:", importlib.metadata.version("sglang"))
print("PyTorch:", torch.__version__)
print("PyTorch CUDA runtime:", torch.version.cuda)
assert torch.cuda.is_available(), "CUDA is unavailable in the service environment"
print("GPU:", torch.cuda.get_device_name(0))
print("GPU capability:", torch.cuda.get_device_capability(0))
PY
```

Successful CUDA detection checks the driver/runtime connection. The later OCR
smoke test also checks whether the model's kernels work on this GPU architecture.

### 3. Download the v2 weights

In the same activated service environment:

```bash
hf download baidu/Unlimited-OCR-2 --local-dir model --exclude 'wheel/*'
```

`hf` comes from the Hugging Face Hub dependency. The command downloads the model
to `~/services/unlimited-ocr-v2/model`, allowing the launcher to use an explicit
local directory. If authentication or download limits require it, run
`hf auth login` and retry. See the [official download CLI guide](https://huggingface.co/docs/huggingface_hub/guides/cli#hf-download).

### 4. Create the launcher and API key

Still inside `~/services/unlimited-ocr-v2`, create a configuration file. This
generates a key once and preserves an existing configuration on subsequent runs:

```bash
python - <<'PY'
from pathlib import Path
import secrets

path = Path("service.env")
if path.exists():
    print("Keeping existing service.env; edit it if settings need to change.")
else:
    path.write_text(
        f"OCR_API_KEY={secrets.token_hex(32)}\n"
        "CUDA_VISIBLE_DEVICES=0\n"
        "OCR_BIND=0.0.0.0\n"
        "OCR_PORT=7870\n"
    )
path.chmod(0o600)
PY
```

The following launcher starts with one concurrent request and CUDA graphs
disabled. These are conservative tutorial choices, rather than Baidu's
64-request performance profile. The specialized v2 options come from the
[Baidu recipe](https://github.com/baidu/Unlimited-OCR#sglang); authentication and
graph controls are documented in [SGLang's server arguments](https://docs.sglang.io/docs/advanced_features/server_arguments).

```bash
cat > run-server.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
set -a
source ./service.env
set +a
: "${OCR_API_KEY:?Set OCR_API_KEY in service.env}"

export SGLANG_UOCR_EANABLE_DOC_LIMIT=1
export SGLANG_UOCR_DOC_GUNDAM_PAGE_WINDOW=4
export SGLANG_UOCR_DOC_BASE_PAGE_WINDOW=4
export SGLANG_UOCR_MAX_PAGE_TOKENS=20000
export SGLANG_OPT_UOCR_SAM_REL_POS_BIAS_MB=64
export SGLANG_UOCR_SHM_WRAP_MODEL_DATA=1
export SGLANG_UOCR_SHM_WRAP_MIN_MB=1
export SGLANG_VLM_CACHE_SIZE_MB=0
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONUNBUFFERED=1

exec ./.venv/bin/python -m sglang.launch_server \
  --model-path "$PWD/model" \
  --served-model-name Unlimited-OCR-2 \
  --host "$OCR_BIND" --port "$OCR_PORT" \
  --api-key "$OCR_API_KEY" \
  --chat-template unlimited-ocr \
  --tokenizer-backend fastokens --tokenizer-worker-num 1 \
  --enable-custom-logit-processor \
  --enable-streaming-session --allow-auto-truncate \
  --disable-radix-cache --mm-preprocess-cache-size-mb 0 \
  --max-running-requests 1 --max-prefill-tokens 2048 \
  --mem-fraction-static 0.7 \
  --cuda-graph-backend-decode disabled \
  --cuda-graph-backend-prefill disabled
SH
chmod +x run-server.sh
./run-server.sh
```

`SGLANG_UOCR_EANABLE_DOC_LIMIT` intentionally retains upstream's spelling.
The server handles page windows inside a whole-document request; the notebook
should still submit each native PDF as one request. A four-page window reduces
peak memory relative to the upstream ten-page setting, with less context across
window boundaries.

Leave the launcher in the foreground during the first test and watch its startup
logs. Binding to `0.0.0.0` makes it reachable over the LAN; restrict access to
trusted clients because this deployment enables custom logit processors. To use
an SSH tunnel instead, set `OCR_BIND=127.0.0.1`, restart the server, and forward
the port from the notebook machine:

```bash
ssh -N -L 17870:127.0.0.1:7870 x@172.27.74.16
```

For that tunnel, use `http://127.0.0.1:17870/v1` as the notebook base URL.

### 5. Verify readiness and the served model

Open a second terminal on the GPU server:

```bash
cd "$HOME/services/unlimited-ocr-v2"
source ./service.env

curl --noproxy '*' --fail --show-error --max-time 120 \
  "http://127.0.0.1:$OCR_PORT/health"

curl --noproxy '*' --fail --silent --show-error \
  -H "Authorization: Bearer $OCR_API_KEY" \
  "http://127.0.0.1:$OCR_PORT/v1/models" \
  | python3 -m json.tool
```

Wait for a successful health response and a model entry whose `id` is
`Unlimited-OCR-2`. A live HTTP listener alone does not prove that the model has
finished loading. Check the server logs if health is not ready.

### 6. Connect ribosome and run one temporary smoke test

On the **notebook machine**, use the ribosome environment, not the service venv:

```bash
cd /d/devel/rag/ribosome  # Adjust if your checkout is elsewhere.
uv sync
uv run nbdev-export
```

Update these existing entries in the project-root `.env`, preserving its other
settings. Copy the generated `OCR_API_KEY` value from the server's `service.env`:

```dotenv
UNLIMITED_OCR_BASE_URL=http://172.27.74.16:7870/v1
UNLIMITED_OCR_MODEL=Unlimited-OCR-2
UNLIMITED_OCR_API_KEY=replace-with-the-server-OCR_API_KEY-value
UNLIMITED_OCR_MAX_CONCURRENCY=1
UNLIMITED_OCR_PAGE_CONCURRENCY=1
```

The service API key is separate from any Hugging Face download token. Copy the
key and v2 model setting from the deployment's private `client.env`, replacing
the old entries rather than adding duplicates. The notebook configuration cell
rereads connection settings from `.env` and prefers them over inherited kernel
values; rerun that cell and the model-list cell after editing the file. Library
callers still use `load_dotenv(..., override=False)`, so refresh their process
environment or restart them after changing `.env`.

This test creates one synthetic image under a temporary directory and calls the
same native path as the notebook. It does not discover or rebuild your corpus:

```bash
uv run python - <<'PY'
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
from ribosome.preprocessing.ocr.baidu_unlimited import list_served_models, ocr_folder


async def main():
    models = await list_served_models()
    assert "Unlimited-OCR-2" in models, models
    print("Served models:", models)
    with TemporaryDirectory(prefix="unlimited-ocr-v2-smoke-") as directory:
        root = Path(directory)
        with pymupdf.open() as document:
            page = document.new_page(width=600, height=240)
            page.insert_text((40, 100), "Unlimited OCR 2 smoke test", fontsize=24)
            page.get_pixmap(dpi=150, alpha=False).save(root / "smoke.png")
        results = await ocr_folder(
            root,
            layout_mode="native",
            crop_mode=False,
            max_concurrency=1,
            max_tokens=8192,
            max_page_tokens=20000,
            request_timeout_s=6000,
            show_progress=False,
        )
        assert len(results) == 1, results
        result = results[0]
        assert result.status == "processed", result.error
        layout = json.loads(result.metadata_path.read_text())
        assert layout["pages_total"] == 1 and "native" in layout
        print(result.markdown_path.read_text())
        print("Native v2 smoke test passed; temporary outputs will be removed.")


asyncio.run(main())
PY
```

After it succeeds, open `nbs/03.preprocessing.ocr.baidu_unlimited.ipynb` and run
the configuration and model-list cells. Start with `MAX_CONCURRENCY=1` and
`CROP_MODE=False` on a memory-constrained GPU. Try `CROP_MODE=True` for tiled
encoding after the base-mode test works. Keep `OCR_LAYOUT_MODE="native"`,
`OUTPUT_DIR_NAME=".md_unlimited_v2"`, `RESUME_PARTIAL=True`, and `OVERWRITE=False`.
Run the corpus cell only when you intend to start processing it.

### 7. Keep the server running with a user systemd service

After the foreground test succeeds, stop that foreground server with `Ctrl+C`.
On a server with a systemd user manager, create a unit for the same launcher:

```bash
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/unlimited-ocr-v2.service" <<'UNIT'
[Unit]
Description=Baidu Unlimited OCR 2 SGLang server
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/services/unlimited-ocr-v2
ExecStart=%h/services/unlimited-ocr-v2/run-server.sh
Restart=on-failure
RestartSec=10
TimeoutStopSec=120
KillMode=control-group

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now unlimited-ocr-v2.service
systemctl --user status unlimited-ocr-v2.service
journalctl --user -u unlimited-ocr-v2.service -f
```

The unit loads `service.env` through the launcher, so venv activation is not
required at boot. `Type=simple` tracks the process; use the health check to verify
model readiness. `Restart=on-failure` and `KillMode=control-group` manage the server
and its worker processes. See the [systemd service reference](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).

For startup without an interactive login and persistence after logout, have the
server administrator enable lingering for the service account, for example
`sudo loginctl enable-linger x`. Otherwise, the user manager's lifetime can depend
on login sessions. See [loginctl's enable-linger documentation](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html).

Apply changes to `service.env` or the launcher with:

```bash
systemctl --user restart unlimited-ocr-v2.service
```

To deliberately shut the service down, use
`systemctl --user stop unlimited-ocr-v2.service` instead.

### 8. Troubleshooting and tuning

| Symptom | Check or adjustment |
| --- | --- |
| Unit has a bad setting; `WorkingDirectory= path is not absolute` | Use the updated installer, which writes an unquoted `WorkingDirectory` path. Rerun deployment to replace the old managed unit and reload systemd. |
| `Connection refused` | Check `systemctl --user status`, startup logs, and the configured port. For LAN access, verify `OCR_BIND=0.0.0.0` and routing/firewall rules. For a tunnel, keep the SSH session open. |
| HTTP 401 | The client must send the exact `OCR_API_KEY` from `service.env` as its bearer token. |
| Model not found or the notebook reports v1 | Confirm `/v1/models` advertises `Unlimited-OCR-2`; replace the old `.env` value and restart the notebook kernel. |
| Unknown v2 CLI options or unsupported architecture | Verify the installed SGLang version is `0.5.20.dev13+g75b9b06b02`; use the pinned Baidu wheel and run `python -m sglang.launch_server --help` in its venv. |
| Dependency resolution or import failure | Keep the server venv isolated, check the selected wheel against upstream, and resolve `uv pip check` failures. Do not force an installation with `--no-deps` or mix the separate Transformers-only pins into it. |
| CUDA is unavailable, a driver error, or missing kernel images | Check `nvidia-smi`, `torch.version.cuda`, GPU capability, and the exact exception. A supported driver alone does not guarantee that every custom kernel supports your GPU. |
| CUDA JIT reports an unsupported GCC version while using `/usr/local/cuda` | Rerun the updated installer to prepare its private CUDA 13 toolkit. Check for `CUDA_HOME` or `CUDA_PATH` overrides in `service.env` that still select an older toolkit. |
| Loaded weights leave no GPU memory for the KV cache | Raise `--mem-fraction-static` above the minimum reported in the log. On this 24 GB RTX 4090 the reported minimum was `0.732`; `--mem-fraction-static 0.85` allows room for the cache. Stop the service before redeploying with the new value. |
| CUDA out of memory | Begin with an otherwise available GPU, one request, graphs disabled, and client `CROP_MODE=False`. Lower both page-window values from `4` to `2` if needed. Lowering `--mem-fraction-static` leaves more memory outside the KV pool, but cannot make model weights fit an undersized GPU. |
| Shared-memory allocation failure | Check available `/dev/shm`. A container needs an adequate shared-memory allocation; this tutorial's main path runs directly on the host. |
| Length-limited response or native page-count mismatch | Check logs, `MAX_TOKENS`, and `MAX_PAGE_TOKENS`. The notebook allocates `MAX_TOKENS × page_count` globally. Do not treat incomplete streamed output as a completed document. |
| Repetition or malformed OCR despite successful health checks | Confirm the custom wheel, v2 weights, chat template, and custom-logit-processor flag. Reproduce on the synthetic single page before processing a long PDF. |

Once stable, increase request concurrency gradually and retest GPU memory. If
enabling CUDA graphs, use the matched graph and batch settings in Baidu's
performance recipe rather than changing only one graph limit. Increasing the
page window toward upstream's `10` can retain more cross-page context at a higher
memory cost. Client and server page-token caps should remain consistent.

On 2026-11-13, authenticated health/model checks and the native v2 synthetic
single-page smoke test passed on a 24 GB RTX 4090 with the private CUDA 13
toolkit, page window `4`, and `OCR_MEM_FRACTION_STATIC=0.85`. This validates the
single-page deployment; larger documents still need workload-specific memory
and output checks.
