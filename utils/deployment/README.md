# Inference service deployment

[deploy_unlimited_ocr_v2.py](deploy_unlimited_ocr_v2.py) installs Baidu's pinned
Unlimited OCR 2 SGLang runtime in a dedicated host directory and manages its
authenticated user service. It is a standalone Python 3.10+ script with no
ribosome or adjacent-file dependencies.

The examples below run from the repository root on the **GPU host**, as the
service account without sudo. For the full manual setup and tuning reference,
see [the OCR upgrade tutorial](../../docs/baidu-unlimited-ocr-v2-upgrade-plan.md#tutorial-install-and-run-the-sglang-service).

## Requirements and installed files

A real deployment requires Linux, an NVIDIA GPU visible through `nvidia-smi`,
available GPU memory, a free serving port, and download access to the Python
runtime, packages, wheel, and model. The installer enforces a driver major
version of at least 580 for its CUDA 13 build. This check does not establish that
the selected GPU has enough memory for a particular workload. Default service
management also requires a working `systemctl --user` session.

The installer creates a private Python 3.10 environment, bootstrapping `uv`
inside the service directory if necessary. Its pinned runtime is SGLang
`0.5.20.dev13+g75b9b06b02`, with the wheel's SHA-256 verified before installation;
it also installs `fastokens==0.3.2` and `pymupdf==1.27.2.2`. Model downloads resolve
to a commit saved in the deployment manifest.

The default installation root is `~/services/unlimited-ocr-v2`:

| Path | Contents |
| --- | --- |
| `.venv/` | Service Python environment, separate from the repository environment. |
| `wheel/` | Pinned wheel and its recorded checksum. |
| `model/` | Local `baidu/Unlimited-OCR-2` model files. |
| `cuda/` | CUDA 13 toolkit layout used for JIT compilation and library lookup. |
| `service.env` | Saved server settings and API key; mode `0600`. |
| `client.env` | Notebook client settings, including the API key; mode `0600`. |
| `deployment.json` | Installed wheel/model identity and resolved model revision. |
| `installed-packages.txt` | Installed package versions. |
| `deploy_unlimited_ocr_v2.py` | Installed copy used by the service launcher. |

The user unit is `unlimited-ocr-v2.service`, under
`$XDG_CONFIG_HOME/systemd/user/` or `~/.config/systemd/user/`. The unit launches the
installed script and does not embed the API key.

## Tutorial: preview and deploy

First inspect the proposed deployment. This dry run performs no downloads, file
writes, GPU probes, or service changes:

```sh
python3 utils/deployment/deploy_unlimited_ocr_v2.py deploy \
  --dry-run --gpu 0 --port 7870 --client-host 172.27.74.16
```

Replace `172.27.74.16` with the hostname or IP address notebook clients will use.
`--client-host` takes a host only, without an `http://` prefix or path.

On the selected GPU host, install and start the service, then perform a small
synthetic-image OCR test:

```sh
python3 utils/deployment/deploy_unlimited_ocr_v2.py deploy \
  --gpu 0 --bind 0.0.0.0 --port 7870 \
  --client-host 172.27.74.16 --smoke-test
```

The installer checks the GPU and port before downloads, verifies the wheel,
installs the runtime, creates the CUDA toolkit layout, downloads the model,
writes private configuration and the user unit, and enables/starts the service.
It waits for authenticated `/health` and `/v1/models` responses. The smoke test
also checks native OCR block output and a completed response, using a temporary
synthetic image that is removed afterward.

Successful output names the installation root and private client configuration.
Read `client.env` locally and merge its entries into the notebook project's
existing `.env`, preserving other settings, then restart the notebook kernel.
It provides `UNLIMITED_OCR_BASE_URL` (ending in `/v1`), `UNLIMITED_OCR_MODEL`,
`UNLIMITED_OCR_API_KEY`, and document/page concurrency settings, both initially
one. The installer does not update the project's `.env` or run corpus jobs.

To print client configuration explicitly:

```sh
python3 utils/deployment/deploy_unlimited_ocr_v2.py client-env \
  --client-host 172.27.74.16
```

This command prints the **API key**. Use its output only where that private
configuration is needed.

## Tutorial: copy only the installer to a remote GPU host

The repository itself is not required on the server. From the machine holding
the checkout, substitute your SSH destination and copy the script:

```sh
scp utils/deployment/deploy_unlimited_ocr_v2.py \
  x@172.27.74.16:/tmp/deploy_unlimited_ocr_v2.py
ssh x@172.27.74.16 \
  'python3 /tmp/deploy_unlimited_ocr_v2.py deploy --dry-run --client-host 172.27.74.16'
```

After reviewing that plan, run the deployment on the host:

```sh
ssh x@172.27.74.16 \
  'python3 /tmp/deploy_unlimited_ocr_v2.py deploy --gpu 0 --port 7870 --client-host 172.27.74.16 --smoke-test'
```

Subsequent commands can use the installed copy:

```sh
python3 "$HOME/services/unlimited-ocr-v2/deploy_unlimited_ocr_v2.py" status
```

## Configuration reference

Saved `service.env` values take precedence over new-install defaults. Explicit
CLI options take precedence over saved values. Rerunning `deploy` preserves the
existing API key and model commit unless the corresponding configuration or
revision is deliberately changed.

| Option | New-install default / behavior |
| --- | --- |
| `--install-dir` | `~/services/unlimited-ocr-v2`; choose a dedicated directory rather than a Python project root. |
| `--gpu` | `0`; accepts `nvidia-smi` indices or full GPU UUIDs. |
| `--bind` | `0.0.0.0` |
| `--port` | `7870` |
| `--client-host` | Uses the bind address, mapping wildcard binds to loopback for client URLs. |
| `--model-revision` | Saved commit on reruns; `main` on a new install, resolved to a commit. |
| `--page-window` | `4` |
| `--max-running-requests` | `1` |
| `--mem-fraction-static` | `0.7` |
| `--max-prefill-tokens` | `2048` |
| `--startup-timeout` | `900` seconds, finite and positive. |
| `--smoke-test` | Off; validates synthetic-image OCR after readiness for `deploy` or `check`. |
| `--no-start` | Prepare runtime and user unit without enabling or starting it. |
| `--no-systemd` | Prepare for manual execution; implies `--no-start`. |
| `--skip-gpu-check` | Requires `--no-start` or `--no-systemd`; supports preparation on a build host. |
| `--dry-run` | Supported only for the `deploy` action. |

`OCR_MAX_PAGE_TOKENS` defaults to `20000` in `service.env`; it has no dedicated
CLI flag. Explicit `CUDA_HOME` or `CUDA_PATH` settings in that file select a
different toolkit; otherwise the launcher uses the private `cuda/` layout.
Changing inherited shell CUDA variables does not override that selection.

## Tutorial: operate and update the service

```sh
python3 utils/deployment/deploy_unlimited_ocr_v2.py status
python3 utils/deployment/deploy_unlimited_ocr_v2.py check --smoke-test
python3 utils/deployment/deploy_unlimited_ocr_v2.py logs
```

`logs` follows the user journal until interrupted. `check` waits for
authenticated readiness; `start` and `restart` also wait for readiness. To
reinstall or change runtime settings, stop the existing instance first:

```sh
python3 utils/deployment/deploy_unlimited_ocr_v2.py stop
python3 utils/deployment/deploy_unlimited_ocr_v2.py deploy \
  --gpu 0 --page-window 2 --client-host 172.27.74.16
```

Deployment and runtime locks prevent modification of an actively managed
environment. Completed downloads and existing configuration survive failures
or interruption, so correct the reported problem and rerun. The installer does
not change NVIDIA drivers, firewall rules, or other inference services. For
service operation after logout or at boot, arrange user lingering with the host
administrator.

For a host without a systemd user manager:

```sh
python3 utils/deployment/deploy_unlimited_ocr_v2.py deploy --no-systemd
python3 "$HOME/services/unlimited-ocr-v2/deploy_unlimited_ocr_v2.py" run
```

The manual `run` action occupies that terminal and holds the runtime lock.
After starting it, use a second terminal for `check --smoke-test`. With a custom
installation root, repeat `--install-dir /path/to/service` on all actions.
`--smoke-test` cannot be combined with a deployment that has `--no-start`;
validate after the service is running instead.

## Validation and troubleshooting

Inspect all options without changing the host:

```sh
python3 utils/deployment/deploy_unlimited_ocr_v2.py --help
```

Run the mocked installer checks in the repository environment; they do not
download runtime packages or operate real services:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest --confcutdir=tests -q \
  tests/test_unlimited_ocr_v2_deployment.py
```

| Symptom | Next step |
| --- | --- |
| GPU or driver preflight error | Check `nvidia-smi` and the selected index/UUID on the actual serving host. |
| Address/port cannot bind | Choose a free port or stop the process already listening there. |
| User systemd unavailable | Use `--no-systemd` and manual `run`, or configure a user manager on the host. |
| Service or deployment lock held | Stop the managed serving process before reinstalling; do not bypass the lock. |
| Wheel checksum mismatch | Confirm the cached wheel is the pinned build; an arbitrary replacement release is not equivalent. |
| Readiness timeout or JIT failure | Inspect `journalctl --user -u unlimited-ocr-v2.service -f`, free GPU memory, and the private CUDA toolkit. |
| Authentication failure | Check the saved `OCR_API_KEY` and merge the matching `client.env` values into the client configuration. |

[Back to the utility guide](../README.md)
