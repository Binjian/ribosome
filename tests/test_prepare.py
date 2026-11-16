"""Profile selection, clean-checkout exports, and notebook execution isolation."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from ribosome.prepare import BACKEND_MODULES, prepare


@pytest.fixture
def checkout(tmp_path):
    source = tmp_path / "source"
    notebooks = source / "nbs"
    notebooks.mkdir(parents=True)
    for notebook, module in {**BACKEND_MODULES, "extra.ipynb": "extra.component"}.items():
        data = {
            "nbformat": 4, "nbformat_minor": 5, "metadata": {},
            "cells": [
                {"cell_type": "code", "id": "module", "metadata": {}, "outputs": [], "execution_count": None,
                 "source": [f"#| default_exp {module}\n"]},
                {"cell_type": "code", "id": "api", "metadata": {}, "outputs": [], "execution_count": None,
                 "source": ["#| export\n", "VALUE = 42\n"]},
                {"cell_type": "code", "id": "example", "metadata": {}, "outputs": [], "execution_count": None,
                 "source": ["raise RuntimeError('Notebook examples must never execute')\n"]},
            ],
        }
        (notebooks / notebook).write_text(json.dumps(data))
    return source


@pytest.mark.parametrize("profile", ["standalone", "llm-backend"])
def test_profiles_export_repeatably_without_executing_notebooks(checkout, tmp_path, profile):
    destination = tmp_path / "output"
    destination.mkdir()
    initializer = destination / "__init__.py"
    initializer.write_text('__version__ = "test"\n')
    prepare(checkout, destination, profile=profile)
    first = {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*.py")}
    prepare(checkout, destination, profile=profile)
    assert first == {p.relative_to(destination): p.read_bytes() for p in destination.rglob("*.py")}
    assert initializer.read_text() == '__version__ = "test"\n'
    assert len([p for p in first if p.name != "__init__.py"]) == len(BACKEND_MODULES) + (profile == "standalone")
    assert (Path("extra/component.py") in first) == (profile == "standalone")
    for path in destination.rglob("*.py"):
        namespace = {}
        exec(path.read_text(), namespace)
        if path.name != "__init__.py":
            assert namespace["VALUE"] == 42


def test_environment_default_and_explicit_profile_precedence(checkout, tmp_path, monkeypatch):
    monkeypatch.delenv("RIBOSOME_PROFILE", raising=False)
    prepare(checkout, tmp_path / "default")
    assert (tmp_path / "default/extra/component.py").is_file()
    monkeypatch.setenv("RIBOSOME_PROFILE", "llm-backend")
    prepare(checkout, tmp_path / "backend")
    assert not (tmp_path / "backend/extra").exists()
    prepare(checkout, tmp_path / "explicit", profile="standalone")
    assert (tmp_path / "explicit/extra/component.py").is_file()
    monkeypatch.setenv("RIBOSOME_PROFILE", "invalid")
    with pytest.raises(ValueError, match="Unknown Ribosome profile"):
        prepare(checkout, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("problem", ["missing", "invalid-json", "wrong-module"])
def test_invalid_backend_sources_fail_before_writing_exports(checkout, tmp_path, problem):
    notebook = checkout / "nbs/01.rag.object.ipynb"
    if problem == "missing":
        notebook.unlink()
    elif problem == "invalid-json":
        notebook.write_text("<<<<<<< HEAD\n")
    else:
        notebook.write_text(notebook.read_text().replace("rag.object", "unexpected.module"))
    destination = tmp_path / "output"
    with pytest.raises((ValueError, FileNotFoundError)):
        prepare(checkout, destination, profile="llm-backend")
    assert not destination.exists()


def test_module_cli_from_another_working_directory(checkout, tmp_path, monkeypatch):
    monkeypatch.setenv("RIBOSOME_PROFILE", "invalid")
    destination = tmp_path / "output"
    result = subprocess.run(
        [sys.executable, "-m", "ribosome.prepare", "--source", str(checkout),
         "--destination", str(destination), "--profile", "llm-backend"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == str(destination)
    assert (destination / "rag/object.py").is_file()
    assert not (destination / "extra").exists()


def test_standalone_exports_all_real_source_modules(tmp_path):
    destination = tmp_path / "ribosome"
    prepare(destination=destination, profile="standalone")
    assert (destination / "rag/object.py").is_file()
    assert (destination / "core/dom/summary/task.py").is_file()
    assert (destination / "preprocessing/ocr/layout_bundle.py").is_file()
    assert (destination / "application/process_parameters.py").is_file()
    for path in destination.rglob("*.py"):
        compile(path.read_text(), str(path), "exec")
