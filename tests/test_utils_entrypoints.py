"""Grouped utilities keep working when launched outside the checkout directory."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("relative", [
    "corpus/copy_remaining_pdfs.py",
    "deployment/deploy_unlimited_ocr_v2.py",
    "krl/collect_krl_qa.py",
    "krl/solve_captcha.py",
    "rag/generate_program_rag_samples.py",
    "rag/generate_regular_object_samples.py",
    "rag/rag_evaluation_samples.py",
    "rag/review_object_rag_evaluation.py",
    "rag/run_object_rag_evaluation.py",
    "summary/benchmark_summary_asyncio.py",
])
def test_direct_cli_help_from_another_directory(relative, tmp_path):
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "utils" / relative), "--help"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert not list(tmp_path.iterdir())


def test_krl_defaults_still_point_to_corpus_in_repository():
    from utils.krl.collect_krl_qa import build_parser

    args = build_parser().parse_args([])
    assert args.questions == ROOT / "assets/KRL/krl_questions_1000.md"
    assert args.output == ROOT / "assets/KRL/krl_qa_collected.md"


def test_krl_direct_dry_run_loads_sibling_store_without_creating_outputs(tmp_path):
    questions = tmp_path / "questions.md"
    questions.write_text("## 0019 真空搬运\n\n完整问题。\n", encoding="utf-8")
    output = tmp_path / "result" / "collected.md"
    result = subprocess.run(
        [sys.executable, str(ROOT / "utils/krl/collect_krl_qa.py"),
         "--dry-run", "--questions", str(questions), "--output", str(output)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "pending: 1" in result.stdout
    assert "0019 真空搬运" in result.stdout
    assert not output.parent.exists()


def test_imported_rag_defaults_do_not_depend_on_working_directory(tmp_path):
    environment = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [sys.executable, "-c", "import json; from utils.rag.run_object_rag_evaluation "
         "import ROOT, NOTEBOOK; print(json.dumps([str(ROOT), str(NOTEBOOK)]))"],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    root, notebook = json.loads(result.stdout)
    assert Path(root) == ROOT
    assert Path(notebook).parent == ROOT / "nbs"
