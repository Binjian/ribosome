#!/usr/bin/env python3
"""Install Baidu's pinned SGLang build and manage its host-local user service.

Run with Python 3.10+ on the GPU host; no ribosome or third-party imports needed.
Upstream: https://github.com/baidu/Unlimited-OCR#sglang
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from email.parser import Parser
from pathlib import Path

MODEL_ID = "baidu/Unlimited-OCR-2"
SERVED_MODEL = "Unlimited-OCR-2"
SGLANG_VERSION = "0.5.20.dev13+g75b9b06b02"
WHEEL_NAME = f"sglang-{SGLANG_VERSION}-py3-none-any.whl"
WHEEL_URL = (
    "https://huggingface.co/baidu/Unlimited-OCR-2/resolve/main/wheel/"
    + WHEEL_NAME.replace("+", "%2B")
)
# Official wheel downloaded and inspected on 2026-11-13. Fail closed on changes.
WHEEL_SHA256 = "576c71dab3cacad843f6870739494e13b2e8169a69cc038f84395d6503e801ea"
SERVICE = "unlimited-ocr-v2.service"
MANAGED = "# Managed by deploy_unlimited_ocr_v2.py"
DEFAULTS = {
    "CUDA_VISIBLE_DEVICES": "0",
    "OCR_BIND": "0.0.0.0",
    "OCR_PORT": "7870",
    "OCR_PAGE_WINDOW": "4",
    "OCR_MAX_RUNNING_REQUESTS": "1",
    "OCR_MEM_FRACTION_STATIC": "0.7",
    "OCR_MAX_PREFILL_TOKENS": "2048",
    "OCR_MAX_PAGE_TOKENS": "20000",
}
OPTIONS = {
    "gpu": "CUDA_VISIBLE_DEVICES",
    "bind": "OCR_BIND",
    "port": "OCR_PORT",
    "page_window": "OCR_PAGE_WINDOW",
    "max_running_requests": "OCR_MAX_RUNNING_REQUESTS",
    "mem_fraction_static": "OCR_MEM_FRACTION_STATIC",
    "max_prefill_tokens": "OCR_MAX_PREFILL_TOKENS",
}


class DeploymentError(RuntimeError):
    """An actionable installation or readiness failure."""


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = data.encode() if isinstance(data, str) else data
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_config(path: Path) -> dict[str, str]:
    """Read assignments as data, never execute or expand shell expressions."""
    values = {}
    if path.exists():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            key, separator, raw = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise DeploymentError(f"{path}:{number}: expected KEY=VALUE")
            try:
                tokens = shlex.split(raw, comments=True)
            except ValueError as error:
                raise DeploymentError(f"{path}:{number}: invalid quoting") from error
            if len(tokens) > 1:
                raise DeploymentError(
                    f"{path}:{number}: quote values containing spaces"
                )
            values[key] = tokens[0] if tokens else ""
    return values


def configuration(args: argparse.Namespace, root: Path) -> dict[str, str]:
    values = {**DEFAULTS, **read_config(root / "service.env")}
    for option, key in OPTIONS.items():
        if getattr(args, option, None) is not None:
            values[key] = str(getattr(args, option))
    if not values.get("OCR_API_KEY"):
        if args.action != "deploy":
            raise DeploymentError(
                "service.env has no OCR_API_KEY; run deploy to create one"
            )
        values["OCR_API_KEY"] = (
            "<generated-on-deploy>" if args.dry_run else secrets.token_hex(32)
        )
    validate_config(values)
    return values


def validate_config(values: dict[str, str]) -> None:
    for key in (
        "OCR_PORT",
        "OCR_PAGE_WINDOW",
        "OCR_MAX_RUNNING_REQUESTS",
        "OCR_MAX_PREFILL_TOKENS",
        "OCR_MAX_PAGE_TOKENS",
    ):
        if not values[key].isdigit() or int(values[key]) < 1:
            raise DeploymentError(f"{key} must be a positive integer")
    if int(values["OCR_PORT"]) > 65535:
        raise DeploymentError("OCR_PORT must be between 1 and 65535")
    try:
        fraction = float(values["OCR_MEM_FRACTION_STATIC"])
    except ValueError as error:
        raise DeploymentError("OCR_MEM_FRACTION_STATIC must be a number") from error
    if not 0 < fraction < 1:
        raise DeploymentError("OCR_MEM_FRACTION_STATIC must be between 0 and 1")
    if not values["CUDA_VISIBLE_DEVICES"] or not values["OCR_BIND"]:
        raise DeploymentError("GPU selection and bind address must not be empty")
    if any(
        "\n" in value or "\r" in value or "\0" in value for value in values.values()
    ):
        raise DeploymentError(
            "Configuration values must not contain control characters"
        )


def write_config(root: Path, values: dict[str, str]) -> None:
    text = "# Local settings; rerunning deploy preserves values unless overridden.\n"
    text += "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    atomic_write(root / "service.env", text)


def run_command(argv, *, capture=False, env=None, check=True, cwd=None):
    argv = [str(value) for value in argv]
    shown = list(argv)
    if "--api-key" in shown:
        shown[shown.index("--api-key") + 1] = "<redacted>"
    print("+ " + shlex.join(shown), flush=True)
    result = subprocess.run(argv, text=True, capture_output=capture, env=env, cwd=cwd)
    if check and result.returncode:
        # Do not include captured server output or credentials in exception text.
        raise DeploymentError(
            f"{Path(argv[0]).name} failed with exit code {result.returncode}"
        )
    return result


def download(url: str, target: Path) -> None:
    """Publish a complete download atomically; retain the old file on failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            with urllib.request.urlopen(url, timeout=60) as response:
                shutil.copyfileobj(response, handle)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_wheel(path: Path, expected_sha256: str = WHEEL_SHA256) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise DeploymentError(
            f"Wheel SHA-256 mismatch for {path}; expected the pinned Baidu build"
        )
    try:
        with zipfile.ZipFile(path) as archive:
            name = next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            )
            metadata = Parser().parsestr(archive.read(name).decode())
    except (zipfile.BadZipFile, StopIteration) as error:
        raise DeploymentError(
            "The downloaded wheel has no valid package metadata"
        ) from error
    if metadata["Name"] != "sglang" or metadata["Version"] != SGLANG_VERSION:
        raise DeploymentError("The wheel is not the expected Baidu SGLang version")


def unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd" / "user" / SERVICE


def systemd_quote(value: str) -> str:
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    )


def service_unit(root: Path) -> str:
    command = [
        root / ".venv/bin/python",
        root / "deploy_unlimited_ocr_v2.py",
        "run",
        "--install-dir",
        root,
    ]
    return (
        f"{MANAGED}\n[Unit]\nDescription=Baidu Unlimited OCR 2 SGLang server\n"
        "After=network-online.target\n\n[Service]\nType=exec\n"
        # WorkingDirectory takes a literal path, not a quoted command argument.
        # Quotes are part of the path here; only systemd specifiers need escaping.
        f"WorkingDirectory={str(root).replace('%', '%%')}\n"
        f"ExecStart={' '.join(systemd_quote(str(item)) for item in command)}\n"
        "Restart=on-failure\nRestartSec=10\nTimeoutStopSec=120\n"
        "KillMode=control-group\nEnvironment=PYTHONUNBUFFERED=1\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def check_unit_owner(path: Path, root: Path) -> None:
    if path.exists():
        text = path.read_text()
        ours = MANAGED in text and systemd_quote(str(root)) in text
        tutorial = f"ExecStart={root}/run-server.sh" in text or (
            root == Path.home() / "services/unlimited-ocr-v2"
            and "ExecStart=%h/services/unlimited-ocr-v2/run-server.sh" in text
        )
        if not ours and not tutorial:
            raise DeploymentError(f"Refusing to replace another service's unit: {path}")


def preflight(args, root: Path, values: dict[str, str]) -> None:
    if sys.platform != "linux":
        raise DeploymentError("Host deployment requires Linux")
    if not args.no_systemd:
        if os.geteuid() == 0:
            raise DeploymentError(
                "Run as the service account without sudo, or use --no-systemd --no-start"
            )
        check_unit_owner(unit_path(), root)
        run_command(["systemctl", "--user", "show-environment"], capture=True)
        active = run_command(
            ["systemctl", "--user", "is-active", "--quiet", SERVICE],
            capture=True,
            check=False,
        )
        if active.returncode == 0:
            raise DeploymentError(
                "The OCR service is running; stop it before changing its environment"
            )
    if not args.skip_gpu_check:
        if not shutil.which("nvidia-smi"):
            raise DeploymentError(
                "nvidia-smi is missing; install a supported NVIDIA driver first"
            )
        result = run_command(
            [
                "nvidia-smi",
                "--query-gpu=index,uuid,driver_version,name,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture=True,
        )
        devices = list(csv.reader(io.StringIO(result.stdout)))
        selection = values["CUDA_VISIBLE_DEVICES"].split(",")
        selected = [
            row
            for row in devices
            if row and (row[0].strip() in selection or row[1].strip() in selection)
        ]
        if not selected or len(selected) != len(selection):
            raise DeploymentError(
                "GPU selection does not match nvidia-smi indices or full UUIDs"
            )
        for row in selected:
            if int(row[2].strip().split(".")[0]) < 580:
                raise DeploymentError(
                    "The pinned wheel needs a CUDA 13 compatible driver (580+ baseline)"
                )
            print(f"GPU {row[0].strip()}: {row[3].strip()}, {row[4].strip()} MiB free")
    if not args.no_start:
        try:
            addresses = socket.getaddrinfo(
                values["OCR_BIND"], int(values["OCR_PORT"]), type=socket.SOCK_STREAM
            )
            family, kind, protocol, _, address = addresses[0]
            with socket.socket(family, kind, protocol) as listener:
                listener.bind(address)
        except OSError as error:
            raise DeploymentError(f"Cannot bind OCR address/port: {error}") from error


@contextmanager
def deployment_lock(root: Path, filename=".deploy.lock", *, inherit=False):
    import fcntl

    with (root / filename).open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeploymentError(
                f"Another process holds {filename}; stop the existing service or installer first"
            ) from error
        os.set_inheritable(handle.fileno(), inherit)
        yield


def ensure_uv(root: Path) -> Path:
    existing = shutil.which("uv")
    private = root / ".tools" / "uv"
    if existing or private.is_file():
        return Path(existing) if existing else private
    installer = root / ".uv-install.sh"
    download("https://astral.sh/uv/install.sh", installer)
    try:
        run_command(
            ["sh", installer],
            env={
                **os.environ,
                "UV_INSTALL_DIR": str(private.parent),
                "UV_NO_MODIFY_PATH": "1",
            },
        )
    finally:
        installer.unlink(missing_ok=True)
    if not private.is_file():
        raise DeploymentError("uv bootstrap did not create its executable")
    return private


VERSION_PROBE = """import importlib.metadata, json
print(json.dumps({'sglang': importlib.metadata.version('sglang')}))
"""
GPU_PROBE = """import torch
assert torch.cuda.is_available(), 'CUDA is unavailable in the service environment'
print('PyTorch:', torch.__version__, 'CUDA:', torch.version.cuda)
print('GPU:', torch.cuda.get_device_name(0), 'capability:', torch.cuda.get_device_capability(0))
"""
REVISION_PROBE = """import sys
from huggingface_hub import HfApi
print(HfApi().model_info('baidu/Unlimited-OCR-2', revision=sys.argv[1]).sha)
"""
CUDA_TOOLKIT_PROBE = """import sysconfig
from pathlib import Path
print(Path(sysconfig.get_path('purelib')) / 'nvidia' / 'cu13')
"""


def prepare_cuda_toolkit(root: Path, toolkit: Path) -> Path:
    """Expose pip's CUDA 13 toolkit in the layout expected by SGLang's JIT."""
    required = (
        "bin/nvcc",
        "bin/ptxas",
        "include/cuda_runtime.h",
        "include/crt/host_config.h",
        "include/cccl",
        "nvvm/bin/cicc",
        "nvvm/libdevice",
        "lib/libcudart.so.13",
    )
    missing = [name for name in required if not (toolkit / name).exists()]
    if missing:
        raise DeploymentError(
            f"Incomplete NVIDIA CUDA 13 JIT toolkit in {toolkit}: {', '.join(missing)}; "
            "repair the service environment's NVIDIA CUDA 13 packages and rerun deploy"
        )

    cuda = root / "cuda"
    libraries = cuda / "lib64"
    for directory in (cuda, libraries):
        if directory.is_symlink():
            raise DeploymentError(
                f"Refusing to modify a linked CUDA directory: {directory}"
            )
        directory.mkdir(exist_ok=True)

    def link(target: Path, path: Path) -> None:
        if path.is_symlink():
            if path.resolve() == target.resolve():
                return
            path.unlink()
        elif path.exists():
            raise DeploymentError(f"Refusing to replace a CUDA toolkit file: {path}")
        path.symlink_to(target, target_is_directory=target.is_dir())

    for name in ("bin", "include", "nvvm"):
        link(toolkit / name, cuda / name)
    for library in (toolkit / "lib").iterdir():
        link(library, libraries / library.name)
    # Wheels ship the SONAME, while the JIT linker requests -lcudart.
    link(toolkit / "lib/libcudart.so.13", libraries / "libcudart.so")
    link(libraries, cuda / "lib")
    return cuda


def install_runtime(args, root: Path, values: dict[str, str]) -> dict:
    # uv must not discover the notebook project's Python/config from our caller's cwd.
    def execute(command, **kwargs):
        return run_command(command, cwd=root, **kwargs)

    uv = ensure_uv(root)
    wheel = root / "wheel" / WHEEL_NAME
    if not wheel.exists():
        print("Downloading Baidu's pinned custom wheel")
        download(WHEEL_URL, wheel)
    verify_wheel(wheel)
    atomic_write(
        wheel.parent / "installed-wheel.sha256", f"{WHEEL_SHA256}  {WHEEL_NAME}\n"
    )
    venv = root / ".venv"
    python = venv / "bin/python"
    if not python.exists():
        execute([uv, "python", "install", "3.10"])
        execute([uv, "venv", "--python", "3.10", venv])
    execute(
        [
            uv,
            "pip",
            "install",
            "--python",
            python,
            "--prerelease=allow",
            wheel,
            "fastokens==0.3.2",
            "pymupdf==1.27.2.2",
        ]
    )
    execute([uv, "pip", "check", "--python", python])
    toolkit = execute([python, "-c", CUDA_TOOLKIT_PROBE], capture=True)
    prepare_cuda_toolkit(root, Path(toolkit.stdout.strip()))
    installed = execute([python, "-c", VERSION_PROBE], capture=True)
    if json.loads(installed.stdout)["sglang"] != SGLANG_VERSION:
        raise DeploymentError(
            "The installed SGLang version does not match the pinned wheel"
        )
    if not args.skip_gpu_check:
        execute(
            [python, "-c", GPU_PROBE],
            env=launch_command(root, values)[1],
        )
    frozen = execute([uv, "pip", "freeze", "--python", python], capture=True)
    atomic_write(root / "installed-packages.txt", frozen.stdout)
    manifest_path = root / "deployment.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    revision = args.model_revision or old.get("model_revision") or "main"
    resolved = execute(
        [python, "-c", REVISION_PROBE, revision],
        capture=True,
        env={**os.environ, **values},
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise DeploymentError("Hugging Face returned an invalid model revision")
    execute(
        [
            venv / "bin/hf",
            "download",
            MODEL_ID,
            "--revision",
            resolved,
            "--local-dir",
            root / "model",
            "--exclude",
            "wheel/*",
        ],
        env={**os.environ, **values},
    )
    if not (root / "model/config.json").is_file():
        raise DeploymentError("Model download did not produce config.json")
    return {
        "sglang_version": SGLANG_VERSION,
        "wheel_sha256": WHEEL_SHA256,
        "model_id": MODEL_ID,
        "model_revision": resolved,
    }


def launch_command(
    root: Path, values: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    env = {
        **os.environ,
        **values,
        "SGLANG_UOCR_EANABLE_DOC_LIMIT": "1",
        "SGLANG_UOCR_DOC_GUNDAM_PAGE_WINDOW": values["OCR_PAGE_WINDOW"],
        "SGLANG_UOCR_DOC_BASE_PAGE_WINDOW": values["OCR_PAGE_WINDOW"],
        "SGLANG_UOCR_MAX_PAGE_TOKENS": values["OCR_MAX_PAGE_TOKENS"],
        "SGLANG_OPT_UOCR_SAM_REL_POS_BIAS_MB": "64",
        "SGLANG_UOCR_SHM_WRAP_MODEL_DATA": "1",
        "SGLANG_UOCR_SHM_WRAP_MIN_MB": "1",
        "SGLANG_VLM_CACHE_SIZE_MB": "0",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        "PYTHONUNBUFFERED": "1",
    }
    # Persist explicit toolkit choices in service.env; inherited shell settings
    # must not select an older global toolkit instead of this service's CUDA 13.
    cuda = Path(values.get("CUDA_HOME") or values.get("CUDA_PATH") or root / "cuda")
    env["CUDA_HOME"] = env["CUDA_PATH"] = str(cuda)
    env["PATH"] = os.pathsep.join(
        [str(root / ".venv/bin"), str(cuda / "bin"), env.get("PATH", os.defpath)]
    )
    env["LD_LIBRARY_PATH"] = os.pathsep.join(
        part for part in (str(cuda / "lib64"), env.get("LD_LIBRARY_PATH")) if part
    )
    command = [
        str(root / ".venv/bin/python"),
        "-m",
        "sglang.launch_server",
        "--model-path",
        str(root / "model"),
        "--served-model-name",
        SERVED_MODEL,
        "--host",
        values["OCR_BIND"],
        "--port",
        values["OCR_PORT"],
        "--api-key",
        values["OCR_API_KEY"],
        "--chat-template",
        "unlimited-ocr",
        "--tokenizer-backend",
        "fastokens",
        "--tokenizer-worker-num",
        "1",
        "--enable-custom-logit-processor",
        "--enable-streaming-session",
        "--allow-auto-truncate",
        "--disable-radix-cache",
        "--mm-preprocess-cache-size-mb",
        "0",
        "--max-running-requests",
        values["OCR_MAX_RUNNING_REQUESTS"],
        "--max-prefill-tokens",
        values["OCR_MAX_PREFILL_TOKENS"],
        "--mem-fraction-static",
        values["OCR_MEM_FRACTION_STATIC"],
        "--cuda-graph-backend-decode",
        "disabled",
        "--cuda-graph-backend-prefill",
        "disabled",
    ]
    return command, env


def base_url(values, host=None):
    host = host or values["OCR_BIND"]
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if any(character in host for character in "/?#@\r\n") or not host:
        raise DeploymentError(
            "Client host must be a hostname or IP address, without a scheme or path"
        )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{values['OCR_PORT']}"


def client_environment(values, host=None):
    entries = {
        "UNLIMITED_OCR_BASE_URL": base_url(values, host) + "/v1",
        "UNLIMITED_OCR_MODEL": SERVED_MODEL,
        "UNLIMITED_OCR_API_KEY": values["OCR_API_KEY"],
        "UNLIMITED_OCR_MAX_CONCURRENCY": "1",
        "UNLIMITED_OCR_PAGE_CONCURRENCY": "1",
    }
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in entries.items())


def wait_ready(values, timeout: float) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last_error = "server has not responded"
    while time.monotonic() < deadline:
        try:
            remaining = max(0.1, min(10, deadline - time.monotonic()))
            headers = {"Authorization": "Bearer " + values["OCR_API_KEY"]}
            for endpoint in ("/health", "/v1/models"):
                request = urllib.request.Request(
                    base_url(values) + endpoint, headers=headers
                )
                with opener.open(request, timeout=remaining) as response:
                    payload = response.read()
            models = json.loads(payload)
            if not isinstance(models, dict) or not isinstance(models.get("data"), list):
                raise DeploymentError("Endpoint returned an invalid OpenAI model list")
            if any(not isinstance(model, dict) for model in models["data"]):
                raise DeploymentError("Endpoint returned an invalid OpenAI model list")
            if SERVED_MODEL not in [
                model.get("id") for model in models.get("data", [])
            ]:
                raise DeploymentError(
                    "Endpoint is reachable but does not serve Unlimited-OCR-2"
                )
            print("Ready: authenticated health and model checks passed")
            return
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise DeploymentError(
                    "OCR API authentication failed; check service.env"
                ) from error
            last_error = f"HTTP {error.code}"
        except (OSError, ValueError) as error:
            last_error = type(error).__name__
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise DeploymentError(
        f"Readiness timed out ({last_error}); inspect journalctl --user -u {SERVICE}"
    )


SMOKE_PROBE = """import base64, json, os, tempfile, urllib.request
from pathlib import Path
import pymupdf
with tempfile.TemporaryDirectory(prefix='unlimited-ocr-v2-smoke-') as directory:
    image = Path(directory) / 'smoke.png'
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=240)
        page.insert_text((40, 100), 'Unlimited OCR 2 smoke test', fontsize=24)
        page.get_pixmap(dpi=150, alpha=False).save(image)
    processor = (b'csglang.srt.sampling.custom_logit_processor\\n'
                 b'DeepseekOCRNoRepeatNGramLogitProcessor\\n.')
    payload = {
        'model': 'Unlimited-OCR-2', 'temperature': 0, 'max_tokens': 8192,
        'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image.read_bytes()).decode()}},
            {'type': 'text', 'text': '<PAGE><image>Multi page parsing.'}]}],
        'skip_special_tokens': False, 'images_config': {'crop_mode': False},
        'custom_logit_processor': json.dumps({'callable': processor.hex()}),
        'custom_params': {'ngram_size': 35, 'window_size': 128, 'page_token_id': 128827,
                          'eos_token_id': 1, 'max_page_tokens': int(os.environ['OCR_MAX_PAGE_TOKENS'])}}
    request = urllib.request.Request(os.environ['OCR_CHECK_URL'] + '/v1/chat/completions',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + os.environ['OCR_API_KEY']})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=6000) as response:
        choice = json.load(response)['choices'][0]
    content = choice['message']['content']
    assert choice['finish_reason'] == 'stop', 'OCR smoke test stopped before completion'
    assert '<PAGE>' in content and '<BLOCK>' in content, 'Response is not native v2 block output'
    print('Native v2 OCR smoke test passed; temporary input removed.')
"""


def smoke_test(root, values):
    run_command(
        [root / ".venv/bin/python", "-c", SMOKE_PROBE],
        env={**os.environ, **values, "OCR_CHECK_URL": base_url(values)},
    )


def dry_run(args, root, values):
    print(
        f"Dry run: no downloads, file writes, GPU probes, or service changes.\nInstall directory: {root}"
    )
    print(
        f"Prepare Python 3.10 with uv (privately bootstrap uv if absent).\nVerify {WHEEL_URL}\nSHA-256: {WHEEL_SHA256}"
    )
    print(
        f"Install SGLang {SGLANG_VERSION}, fastokens 0.3.2, PyMuPDF 1.27.2.2; run uv pip check."
    )
    print(
        "Prepare a private CUDA 13 toolkit layout from the installed NVIDIA packages "
        "for JIT compilation."
    )
    print(
        f"Download {MODEL_ID}, revision {args.model_revision or 'saved revision, or main for a new install'}."
    )
    print(
        "Preserve service.env settings and API key; write a private client.env and installed launcher."
    )
    command, _ = launch_command(root, values)
    command[command.index("--api-key") + 1] = "<redacted>"
    print("Launcher: " + shlex.join(command))
    if not args.no_systemd:
        print(f"Write {unit_path()}; reload user systemd.")
        if not args.no_start:
            print(f"Enable/start {SERVICE}; wait for authenticated readiness.")
    if args.smoke_test:
        print("Run synthetic-image OCR smoke test after readiness.")


def deploy(args, root, values):
    if args.dry_run:
        dry_run(args, root, values)
        return
    preflight(args, root, values)
    root.mkdir(parents=True, exist_ok=True)
    with deployment_lock(root):
        # A manual launcher also keeps this lock across exec, so its venv cannot
        # be modified while it is serving. Release it before starting systemd.
        with deployment_lock(root, ".runtime.lock"):
            manifest = install_runtime(args, root, values)
            write_config(root, values)
            atomic_write(
                root / "client.env", client_environment(values, args.client_host)
            )
            atomic_write(
                root / "deploy_unlimited_ocr_v2.py", Path(__file__).read_bytes(), 0o700
            )
            atomic_write(
                root / "deployment.json", json.dumps(manifest, indent=2) + "\n"
            )
        if not args.no_systemd:
            atomic_write(unit_path(), service_unit(root), 0o644)
            run_command(["systemctl", "--user", "daemon-reload"])
            if not args.no_start:
                run_command(["systemctl", "--user", "enable", "--now", SERVICE])
                wait_ready(values, args.startup_timeout)
                if args.smoke_test:
                    smoke_test(root, values)
    print(f"Prepared {root}\nPrivate notebook configuration: {root / 'client.env'}")
    if args.no_systemd:
        print(
            f"Start manually: {shlex.join([str(root / '.venv/bin/python'), str(root / 'deploy_unlimited_ocr_v2.py'), 'run', '--install-dir', str(root)])}"
        )
    else:
        print(f"Logs: journalctl --user -u {SERVICE} -f")
        print(
            "For operation after logout/at boot, have your administrator enable user lingering."
        )


def parser():
    cli = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    cli.add_argument(
        "action",
        nargs="?",
        default="deploy",
        choices=[
            "deploy",
            "run",
            "check",
            "status",
            "start",
            "restart",
            "stop",
            "logs",
            "client-env",
        ],
    )
    cli.add_argument(
        "--install-dir", type=Path, default=Path.home() / "services/unlimited-ocr-v2"
    )
    cli.add_argument(
        "--gpu", help="nvidia-smi index or full GPU UUID; existing value is preserved"
    )
    cli.add_argument("--bind", help="listen address; default 0.0.0.0 on a new install")
    cli.add_argument("--port", type=int)
    cli.add_argument(
        "--client-host",
        help="address to put in client.env; defaults to a local address",
    )
    cli.add_argument(
        "--model-revision",
        help="HF revision; reruns otherwise keep the saved model commit",
    )
    cli.add_argument("--page-window", type=int)
    cli.add_argument("--max-running-requests", type=int)
    cli.add_argument("--mem-fraction-static", type=float)
    cli.add_argument("--max-prefill-tokens", type=int)
    cli.add_argument(
        "--no-start",
        action="store_true",
        help="install/write unit without enabling or starting it",
    )
    cli.add_argument(
        "--no-systemd",
        action="store_true",
        help="prepare files for manual execution; implies --no-start",
    )
    cli.add_argument(
        "--skip-gpu-check",
        action="store_true",
        help="prepare on a build host; requires --no-start",
    )
    cli.add_argument("--startup-timeout", type=float, default=900)
    cli.add_argument(
        "--smoke-test",
        action="store_true",
        help="also OCR a temporary synthetic image after readiness",
    )
    cli.add_argument(
        "--dry-run",
        action="store_true",
        help="show deployment without changing the host",
    )
    return cli


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.no_systemd:
        args.no_start = True
    if args.skip_gpu_check and not args.no_start:
        cli.error("--skip-gpu-check requires --no-start (or --no-systemd)")
    if not math.isfinite(args.startup_timeout) or args.startup_timeout <= 0:
        cli.error("--startup-timeout must be finite and positive")
    if args.smoke_test and args.no_start and args.action == "deploy":
        cli.error(
            "--smoke-test requires a running service; use check --smoke-test after starting it"
        )
    if args.dry_run and args.action != "deploy":
        cli.error("--dry-run is supported for deploy")
    try:
        root = args.install_dir.expanduser().resolve()
        if any(character in str(root) for character in "\n\r\0"):
            raise DeploymentError(
                "Install directory must not contain control characters"
            )
        if args.action == "deploy" and (root / "pyproject.toml").exists():
            raise DeploymentError(
                "Choose a dedicated service directory, not a Python project root"
            )
        if args.action != "deploy" and not (root / "service.env").is_file():
            raise DeploymentError(f"No service.env in {root}; run deploy first")
        values = configuration(args, root)
        if args.client_host:
            base_url(values, args.client_host)
        if args.action == "deploy":
            deploy(args, root, values)
        elif args.action == "run":
            command, env = launch_command(root, values)
            with deployment_lock(root, ".runtime.lock", inherit=True):
                os.chdir(root)
                os.execvpe(command[0], command, env)
        elif args.action == "client-env":
            print(client_environment(values, args.client_host), end="")
        elif args.action == "check":
            wait_ready(values, args.startup_timeout)
            if args.smoke_test:
                smoke_test(root, values)
        else:
            if not unit_path().is_file():
                raise DeploymentError(
                    "No managed user unit found; use run for a manual deployment"
                )
            check_unit_owner(unit_path(), root)
            if args.action == "logs":
                run_command(["journalctl", "--user", "-u", SERVICE, "-f"])
            else:
                run_command(["systemctl", "--user", args.action, SERVICE])
                if args.action in {"start", "restart"}:
                    wait_ready(values, args.startup_timeout)
    except (DeploymentError, OSError, ValueError, KeyError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted; completed downloads and existing configuration are retained.",
            file=sys.stderr,
        )
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
