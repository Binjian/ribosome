# Preparation profiles

Standalone development and llm-backend integration share Ribosome's `master`
branch and reusable APIs. Select an export profile at preparation time:

```sh
uv sync --locked
uv run ribosome-prepare --profile standalone
uv run ribosome-prepare --profile llm-backend
RIBOSOME_PROFILE=llm-backend uv run ribosome-prepare
```

`standalone` is the default for Ribosome's command and exports every notebook
with a `default_exp` module. `llm-backend` exports natural-text embedding,
object embedding, object RAG, and `application.process_parameters` from
`nbs/05.application.process_parameters.ipynb`. The separate process-parameter
tutorial has no export module. `--profile` overrides `RIBOSOME_PROFILE`; unknown
profiles fail before exporting. Applications supply their own clients, model
settings and index/mapping paths through the existing constructors.

Neither profile executes notebook code, loads `.env`, opens indexes, rebuilds
corpus data or calls model services. Imports do not trigger preparation. A profile
adds or refreshes exports without removing existing files. Use a fresh output
directory for an artifact that must contain only the selected modules.

The command works from another working directory and accepts explicit paths:

```sh
python -m ribosome.prepare --profile llm-backend \
  --source /path/to/ribosome --destination /tmp/package/ribosome
```

Preparation needs the source checkout's `nbs/` directory. Prepare the desired
modules before building a wheel; installed wheels do not contain notebooks.
Alternate destinations contain generated modules; combine them with the
checked-in package files when assembling a distribution.
Generated modules remain ignored. Edit notebook sources and regenerate instead
of editing generated modules. `ribosome/prepare.py` is a handwritten build utility,
like `ribosome/ollama_config.py`.

In llm-backend, `tools/prepare_ribosome.py` delegates to this command's shared
implementation and defaults to `llm-backend`. The parent uses an editable path
dependency at `vendors/ribosome` and pins a Git commit from `master`; a profile
does not choose a branch or update the submodule.

To validate a checkout, run `uv run pytest`. Its session hook prepares ignored
exports needed by the complete standalone suite, irrespective of the selected
application preparation profile. Run backend integration tests in llm-backend's
environment after preparing with `--profile llm-backend`.
