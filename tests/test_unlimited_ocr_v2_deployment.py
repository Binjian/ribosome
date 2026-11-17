"""Exercise the host installer without downloading packages or changing services."""

import ast
import hashlib
import io
import json
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from utils.deployment import deploy_unlimited_ocr_v2 as deploy


def args(root, *options):
    result = deploy.parser().parse_args(["--install-dir", str(root), *options])
    if result.no_systemd:
        result.no_start = True
    return result


def wheel(path, version=deploy.SGLANG_VERSION):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "sglang.dist-info/METADATA", f"Name: sglang\nVersion: {version}\n"
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cuda_toolkit(path):
    for name in (
        "bin/nvcc",
        "bin/ptxas",
        "include/cuda_runtime.h",
        "include/crt/host_config.h",
        "nvvm/bin/cicc",
        "lib/libcudart.so.13",
        "lib/libcudart_static.a",
    ):
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(name)
    (path / "include/cccl").mkdir(exist_ok=True)
    (path / "nvvm/libdevice").mkdir(exist_ok=True)
    return path


def test_standalone_dry_run_does_not_change_host_or_print_key(tmp_path):
    root = tmp_path / "new install"
    script = Path(deploy.__file__).resolve()
    result = subprocess.run(
        [
            sys.executable,
            script,
            "deploy",
            "--install-dir",
            root,
            "--dry-run",
            "--gpu",
            "1",
            "--port",
            "7871",
            "--client-host",
            "172.27.74.16",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert not root.exists()
    assert "--port 7871" in result.stdout and "<redacted>" in result.stdout
    assert "no downloads, file writes" in result.stdout
    root.mkdir()
    deploy.atomic_write(root / "service.env", "OCR_API_KEY=private-existing-key\n")
    before = (root / "service.env").read_bytes()
    result = subprocess.run(
        [sys.executable, script, "--install-dir", root, "--dry-run"],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    assert "private-existing-key" not in result.stdout + result.stderr
    assert (root / "service.env").read_bytes() == before
    assert list(root.iterdir()) == [root / "service.env"]


def test_rerun_preserves_settings_key_and_private_permissions(tmp_path):
    first = deploy.configuration(args(tmp_path), tmp_path)
    deploy.write_config(tmp_path, {**first, "OCR_PORT": "9090", "OCR_PAGE_WINDOW": "2"})
    second = deploy.configuration(args(tmp_path, "--gpu", "1"), tmp_path)
    assert second["OCR_API_KEY"] == first["OCR_API_KEY"]
    assert second["OCR_PORT"] == "9090" and second["OCR_PAGE_WINDOW"] == "2"
    assert second["CUDA_VISIBLE_DEVICES"] == "1"
    assert (tmp_path / "service.env").stat().st_mode & 0o777 == 0o600
    assert len(first["OCR_API_KEY"]) == 64


def test_project_environment_cannot_be_used_as_install_directory(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='existing-project'\n")
    assert deploy.main(["--install-dir", str(tmp_path), "--dry-run"]) == 1
    assert not (tmp_path / ".venv").exists()


def test_configuration_is_data_and_does_not_rotate_key_on_start(tmp_path):
    marker = tmp_path / "must-not-exist"
    deploy.atomic_write(tmp_path / "service.env", f"OCR_API_KEY='$(touch {marker})'\n")
    config = deploy.configuration(args(tmp_path, "run"), tmp_path)
    assert config["OCR_API_KEY"] == f"$(touch {marker})"
    assert not marker.exists()
    deploy.atomic_write(tmp_path / "service.env", "OCR_API_KEY=''\n")
    with pytest.raises(deploy.DeploymentError, match="no OCR_API_KEY"):
        deploy.configuration(args(tmp_path, "run"), tmp_path)


@pytest.mark.parametrize(
    "option,value",
    [
        ("--port", "0"),
        ("--port", "65536"),
        ("--page-window", "0"),
        ("--max-running-requests", "-1"),
        ("--mem-fraction-static", "nan"),
        ("--mem-fraction-static", "1"),
        ("--client-host", "http://wrong-host"),
    ],
)
def test_invalid_options_fail_without_creating_directory(tmp_path, option, value):
    root = tmp_path / "absent"
    assert (
        deploy.main(["deploy", "--install-dir", str(root), "--dry-run", option, value])
        == 1
    )
    assert not root.exists()


def test_wheel_identity_and_checksum_are_verified(tmp_path):
    path = tmp_path / deploy.WHEEL_NAME
    digest = wheel(path)
    deploy.verify_wheel(path, digest)
    with pytest.raises(deploy.DeploymentError, match="SHA-256"):
        deploy.verify_wheel(path, "0" * 64)
    wrong = wheel(path, "0.0.1")
    with pytest.raises(deploy.DeploymentError, match="expected Baidu"):
        deploy.verify_wheel(path, wrong)


def test_interrupted_download_preserves_existing_artifact(tmp_path, monkeypatch):
    class BrokenResponse(io.BytesIO):
        def read(self, *_args):
            raise OSError("interrupted")

    path = tmp_path / "wheel.whl"
    path.write_bytes(b"previous complete artifact")
    monkeypatch.setattr(
        deploy.urllib.request, "urlopen", lambda *_args, **_kwargs: BrokenResponse()
    )
    with pytest.raises(OSError, match="interrupted"):
        deploy.download("https://offline.invalid/wheel", path)
    assert path.read_bytes() == b"previous complete artifact"
    assert list(tmp_path.iterdir()) == [path]


class FakeCommands:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def __call__(self, command, **kwargs):
        command = [str(item) for item in command]
        self.calls.append((command, kwargs))
        output = ""
        if "venv" in command:
            binaries = self.root / ".venv/bin"
            binaries.mkdir(parents=True)
            (binaries / "python").touch()
            (binaries / "hf").touch()
        if deploy.VERSION_PROBE in command:
            output = json.dumps({"sglang": deploy.SGLANG_VERSION})
        if deploy.CUDA_TOOLKIT_PROBE in command:
            output = str(
                cuda_toolkit(self.root / ".venv/lib/python3.10/site-packages/nvidia/cu13")
            )
        if deploy.REVISION_PROBE in command:
            output = "b" * 40
        if "download" in command:
            model = self.root / "model"
            model.mkdir(exist_ok=True)
            (model / "config.json").write_text("{}")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


def test_install_only_workflow_is_repeatable_and_pins_model_commit(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "service"
    root.mkdir()
    digest = wheel(root / "wheel" / deploy.WHEEL_NAME)
    verify = deploy.verify_wheel
    monkeypatch.setattr(deploy, "verify_wheel", lambda path: verify(path, digest))
    monkeypatch.setattr(deploy, "ensure_uv", lambda _root: Path("/offline/uv"))
    commands = FakeCommands(root)
    monkeypatch.setattr(deploy, "run_command", commands)
    monkeypatch.setattr(
        deploy, "download", lambda *_args: pytest.fail("Unexpected network download")
    )
    options = [
        "deploy",
        "--install-dir",
        str(root),
        "--no-systemd",
        "--skip-gpu-check",
        "--client-host",
        "172.27.74.16",
    ]
    assert deploy.main(options) == 0
    first = deploy.read_config(root / "service.env")
    manifest = json.loads((root / "deployment.json").read_text())
    assert manifest["model_revision"] == "b" * 40
    assert manifest["wheel_sha256"] == deploy.WHEEL_SHA256
    assert (root / "deploy_unlimited_ocr_v2.py").is_file()
    assert (root / "deploy_unlimited_ocr_v2.py").stat().st_mode & 0o777 == 0o700
    client = deploy.read_config(root / "client.env")
    assert client["UNLIMITED_OCR_MODEL"] == "Unlimited-OCR-2"
    assert client["UNLIMITED_OCR_BASE_URL"] == "http://172.27.74.16:7870/v1"
    assert client["UNLIMITED_OCR_API_KEY"] == first["OCR_API_KEY"]
    assert first["OCR_API_KEY"] not in capsys.readouterr().out
    assert deploy.main(options) == 0
    assert deploy.read_config(root / "service.env") == first
    revisions = [
        command[-1] for command, _ in commands.calls if deploy.REVISION_PROBE in command
    ]
    assert revisions == ["main", "b" * 40]
    assert not any(command[0] == "systemctl" for command, _ in commands.calls)
    assert all(options.get("cwd") == root for _command, options in commands.calls)


def test_failed_install_preserves_existing_configuration(tmp_path, monkeypatch):
    root = tmp_path / "service"
    root.mkdir()
    deploy.atomic_write(root / "service.env", "OCR_API_KEY=old-key\nOCR_PORT=9090\n")
    before = (root / "service.env").read_bytes()
    monkeypatch.setattr(
        deploy,
        "install_runtime",
        lambda *_args: (_ for _ in ()).throw(deploy.DeploymentError("install failed")),
    )
    assert (
        deploy.main(
            [
                "--install-dir",
                str(root),
                "--no-systemd",
                "--skip-gpu-check",
                "--port",
                "9091",
            ]
        )
        == 1
    )
    assert (root / "service.env").read_bytes() == before
    assert not (root / "deployment.json").exists()


def test_default_deployment_registers_starts_and_authenticates(tmp_path, monkeypatch):
    root = tmp_path / "service"
    root.mkdir()
    digest = wheel(root / "wheel" / deploy.WHEEL_NAME)
    verify = deploy.verify_wheel
    monkeypatch.setattr(deploy, "verify_wheel", lambda path: verify(path, digest))
    monkeypatch.setattr(deploy, "ensure_uv", lambda _root: Path("/offline/uv"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "systemd-config"))
    # Preflight OS probes have separate tests; no real GPU, port, or user bus here.
    monkeypatch.setattr(deploy, "preflight", lambda *_args: None)
    commands = FakeCommands(root)

    def execute(command, **kwargs):
        if "systemctl" == str(command[0]) and "enable" in command:
            # The service's run action must be able to acquire the runtime lock.
            with deploy.deployment_lock(root, ".runtime.lock"):
                pass
        return commands(command, **kwargs)

    monkeypatch.setattr(deploy, "run_command", execute)
    opener = FakeOpener()
    monkeypatch.setattr(deploy.urllib.request, "build_opener", lambda *_args: opener)
    assert (
        deploy.main(["--install-dir", str(root), "--skip-gpu-check", "--no-start"]) == 0
    )
    config = deploy.read_config(root / "service.env")
    assert deploy.unit_path().name == deploy.SERVICE
    assert deploy.MANAGED in deploy.unit_path().read_text()
    assert not any("enable" in command for command, _ in commands.calls)
    commands.calls.clear()
    # A normal deployment starts and enables the prepared service.
    assert deploy.main(["--install-dir", str(root)]) == 0
    systemd = [command for command, _ in commands.calls if command[0] == "systemctl"]
    assert systemd == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", deploy.SERVICE],
    ]
    assert len(opener.requests) == 2
    assert (
        opener.requests[1][0].get_header("Authorization")
        == "Bearer " + config["OCR_API_KEY"]
    )
    assert config["OCR_API_KEY"] not in deploy.unit_path().read_text()


def test_missing_user_unit_and_nonfinite_timeout_fail_before_service_changes(
    tmp_path, monkeypatch
):
    deploy.atomic_write(tmp_path / "service.env", "OCR_API_KEY=key\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "missing-config"))
    monkeypatch.setattr(
        deploy,
        "run_command",
        lambda *_args, **_kwargs: pytest.fail("Unexpected service command"),
    )
    assert deploy.main(["stop", "--install-dir", str(tmp_path)]) == 1
    with pytest.raises(SystemExit):
        deploy.main(["--dry-run", "--startup-timeout", "nan"])


def test_launch_protocol_and_unit_keep_credentials_out_of_unit(tmp_path):
    config = {
        **deploy.DEFAULTS,
        "OCR_API_KEY": "secret-key",
        "CUDA_VISIBLE_DEVICES": "1",
    }
    command, env = deploy.launch_command(tmp_path, config)
    assert command[command.index("--served-model-name") + 1] == "Unlimited-OCR-2"
    assert command[command.index("--model-path") + 1] == str(tmp_path / "model")
    assert "--enable-custom-logit-processor" in command
    assert (
        "--enable-streaming-session" in command and "--disable-radix-cache" in command
    )
    assert "--chat-template" in command and "--cuda-graph-backend-prefill" in command
    assert env["CUDA_VISIBLE_DEVICES"] == "1"
    assert env["SGLANG_UOCR_EANABLE_DOC_LIMIT"] == "1"
    assert env["SGLANG_UOCR_DOC_BASE_PAGE_WINDOW"] == "4"
    unit = deploy.service_unit(tmp_path / "space and % directory")
    assert "secret-key" not in unit
    assert "Type=exec" in unit and "KillMode=control-group" in unit
    assert "space and %% directory" in unit
    ast.parse(deploy.SMOKE_PROBE, feature_version=(3, 10))


def test_private_cuda_toolkit_exposes_jit_linker_layout_without_changing_packages(
    tmp_path,
):
    toolkit = cuda_toolkit(tmp_path / "site-packages/nvidia/cu13")
    before = {
        path.relative_to(toolkit): path.read_bytes()
        for path in toolkit.rglob("*")
        if path.is_file()
    }
    cuda = deploy.prepare_cuda_toolkit(tmp_path, toolkit)
    # Both nvcc's profile and SGLang's -L$CUDA_HOME/lib64 -lcudart must work.
    assert (cuda / "bin/nvcc").samefile(toolkit / "bin/nvcc")
    assert (cuda / "include/cccl").samefile(toolkit / "include/cccl")
    assert (cuda / "nvvm/libdevice").samefile(toolkit / "nvvm/libdevice")
    assert (cuda / "lib64/libcudart.so").samefile(toolkit / "lib/libcudart.so.13")
    assert (cuda / "lib/libcudart_static.a").samefile(toolkit / "lib/libcudart_static.a")
    assert deploy.prepare_cuda_toolkit(tmp_path, toolkit) == cuda
    assert before == {
        path.relative_to(toolkit): path.read_bytes()
        for path in toolkit.rglob("*")
        if path.is_file()
    }


def test_incomplete_cuda_toolkit_fails_before_creating_private_layout(tmp_path):
    toolkit = cuda_toolkit(tmp_path / "site-packages/nvidia/cu13")
    (toolkit / "lib/libcudart.so.13").unlink()
    with pytest.raises(deploy.DeploymentError, match="libcudart.so.13.*rerun deploy"):
        deploy.prepare_cuda_toolkit(tmp_path, toolkit)
    assert not (tmp_path / "cuda").exists()


def test_launch_uses_service_cuda_instead_of_inherited_global_toolkit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CUDA_HOME", "/usr/local/cuda-12.1")
    monkeypatch.setenv("CUDA_PATH", "/usr/local/cuda-12.1")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/existing/libraries")
    config = {**deploy.DEFAULTS, "OCR_API_KEY": "key"}
    _, env = deploy.launch_command(tmp_path, config)
    assert env["CUDA_HOME"] == env["CUDA_PATH"] == str(tmp_path / "cuda")
    assert env["PATH"].split(":") == [
        str(tmp_path / ".venv/bin"),
        str(tmp_path / "cuda/bin"),
        "/usr/bin",
    ]
    assert env["LD_LIBRARY_PATH"].split(":") == [
        str(tmp_path / "cuda/lib64"),
        "/existing/libraries",
    ]
    assert not (tmp_path / "cuda").exists()
    # Explicit service.env settings still choose a different toolkit.
    _, custom_env = deploy.launch_command(
        tmp_path, {**config, "CUDA_PATH": "/custom/cuda13"}
    )
    assert custom_env["CUDA_HOME"] == custom_env["CUDA_PATH"] == "/custom/cuda13"
    assert "/custom/cuda13/bin" in custom_env["PATH"].split(":")


@pytest.mark.parametrize(
    "directory,unit_directory",
    [
        ("service", "service"),
        ("space and % directory", "space and %% directory"),
    ],
)
def test_unit_working_directory_is_an_unquoted_literal_path(
    tmp_path, directory, unit_directory
):
    root = tmp_path / directory
    unit = deploy.service_unit(root)
    working_directory = next(
        line for line in unit.splitlines() if line.startswith("WorkingDirectory=")
    )
    assert working_directory == f"WorkingDirectory={tmp_path}/{unit_directory}"
    # ExecStart has its own quoting rules and still identifies our managed unit.
    assert "ExecStart=" + deploy.systemd_quote(str(root / ".venv/bin/python")) in unit
    path = tmp_path / deploy.SERVICE
    path.write_text(unit)
    deploy.check_unit_owner(path, root)


def test_runtime_lock_prevents_environment_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deploy,
        "install_runtime",
        lambda *_args: pytest.fail("Active runtime must block installation"),
    )
    with deploy.deployment_lock(tmp_path, ".runtime.lock", inherit=True):
        assert (
            deploy.main(
                ["--install-dir", str(tmp_path), "--no-systemd", "--skip-gpu-check"]
            )
            == 1
        )


def test_foreign_unit_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy.os, "geteuid", lambda: 1000)
    unit = tmp_path / "config/systemd/user" / deploy.SERVICE
    deploy.atomic_write(unit, "[Service]\nExecStart=/someone/elses/server\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = tmp_path / "absent"
    with pytest.raises(deploy.DeploymentError, match="another service"):
        deploy.preflight(
            args(root, "--no-start", "--skip-gpu-check"),
            root,
            {**deploy.DEFAULTS, "OCR_API_KEY": "key"},
        )
    assert not root.exists()
    assert "someone/elses" in unit.read_text()


def test_active_service_and_old_driver_fail_before_install(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        deploy,
        "run_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=""),
    )
    with pytest.raises(deploy.DeploymentError, match="is running"):
        deploy.preflight(
            args(tmp_path, "--no-start"),
            tmp_path,
            {**deploy.DEFAULTS, "OCR_API_KEY": "key"},
        )
    monkeypatch.setattr(deploy.shutil, "which", lambda *_args: "/offline/nvidia-smi")
    monkeypatch.setattr(
        deploy,
        "run_command",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="0,GPU-123,570.99,Example GPU,24000\n"
        ),
    )
    with pytest.raises(deploy.DeploymentError, match="580"):
        deploy.preflight(
            args(tmp_path, "--no-systemd"),
            tmp_path,
            {**deploy.DEFAULTS, "OCR_API_KEY": "key"},
        )


class FakeOpener:
    def __init__(self, *, models=None, error=None):
        self.models = [deploy.SERVED_MODEL] if models is None else models
        self.error = error
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append((request, kwargs))
        if self.error:
            raise self.error
        data = (
            {"data": [{"id": model} for model in self.models]}
            if request.full_url.endswith("/models")
            else {}
        )
        return io.BytesIO(json.dumps(data).encode())


def test_readiness_authenticates_and_checks_served_model(monkeypatch):
    config = {**deploy.DEFAULTS, "OCR_API_KEY": "private-token"}
    opener = FakeOpener()
    monkeypatch.setattr(deploy.urllib.request, "build_opener", lambda *_args: opener)
    deploy.wait_ready(config, 1)
    assert len(opener.requests) == 2
    assert all(
        request.get_header("Authorization") == "Bearer private-token"
        for request, _ in opener.requests
    )
    opener.models = ["baidu/Unlimited-OCR"]
    with pytest.raises(deploy.DeploymentError, match="does not serve"):
        deploy.wait_ready(config, 1)


def test_auth_failure_and_readiness_timeout_are_actionable(monkeypatch):
    config = {**deploy.DEFAULTS, "OCR_API_KEY": "private-token"}
    opener = FakeOpener(
        error=urllib.error.HTTPError("http://offline", 401, "Unauthorized", {}, None)
    )
    monkeypatch.setattr(deploy.urllib.request, "build_opener", lambda *_args: opener)
    with pytest.raises(deploy.DeploymentError, match="authentication failed") as error:
        deploy.wait_ready(config, 1)
    assert "private-token" not in str(error.value)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(deploy.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(
        deploy.time, "sleep", lambda seconds: setattr(clock, "now", clock.now + seconds)
    )
    opener.error = urllib.error.URLError("offline")
    with pytest.raises(deploy.DeploymentError, match="Readiness timed out"):
        deploy.wait_ready(config, 0.1)


def test_client_env_ipv6_and_plain_key_roundtrip():
    config = {**deploy.DEFAULTS, "OCR_API_KEY": "key"}
    assert deploy.base_url(config, "::1") == "http://[::1]:7870"
    assert (
        "UNLIMITED_OCR_BASE_URL=http://192.0.2.2:7870/v1"
        in deploy.client_environment(config, "192.0.2.2")
    )
