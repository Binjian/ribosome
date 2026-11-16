"""Clean notebook exports and an offline run-all of the process tutorial."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from nbdev.export import nb_export

pytest.importorskip("treesitter4robotcode.dialects", reason="requires optional robot-control dependencies")
pytest.importorskip("robot_language_lean_checker.welding", reason="requires optional robot-control dependencies")

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "nbs/05.application.process_parameters.ipynb"
TUTORIAL = ROOT / "nbs/06.tutorial.process_parameters.ipynb"


@pytest.fixture
def exported_package(tmp_path):
    package = tmp_path / "ribosome"
    (package / "application").mkdir(parents=True)
    for directory in (package, package / "application"):
        (directory / "__init__.py").touch()

    # Export a fresh copy rather than use an installed or stale ignored module.
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    notebook["cells"].append({
        "cell_type": "code", "id": "execution-sentinel", "metadata": {},
        "outputs": [], "execution_count": None,
        "source": ["raise AssertionError('NONEXPORTED_EXAMPLE_MUST_NOT_EXECUTE')\n"],
    })
    source = tmp_path / NOTEBOOK.name
    source.write_text(json.dumps(notebook), encoding="utf-8")
    nb_export(str(source), lib_path=str(package))
    module = package / "application/process_parameters.py"
    assert module.is_file()
    assert "NONEXPORTED_EXAMPLE_MUST_NOT_EXECUTE" not in module.read_text(encoding="utf-8")
    return tmp_path


def isolated_script(exported_package, script, *arguments):
    # -I drops cwd/PYTHONPATH import shortcuts. Only the fresh exported package
    # is inserted, while parser/checker dependencies come from the environment.
    bootstrap = """
import importlib.abc
from pathlib import Path
import socket
import sys

sys.path.insert(0, sys.argv[1])

class NoBackend(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'llm_backend' or fullname.startswith('llm_backend.'):
            raise AssertionError('Ribosome export must not import the backend')

sys.meta_path.insert(0, NoBackend())

def no_network(*args, **kwargs):
    raise AssertionError('Process examples must run without network connections')

socket.socket.connect = no_network
socket.create_connection = no_network
"""
    return subprocess.run(
        [sys.executable, "-I", "-c", bootstrap + script, str(exported_package), *map(str, arguments)],
        cwd=exported_package, capture_output=True, text=True, check=True,
    )


def test_clean_export_imports_and_generates_without_backend_or_example_execution(exported_package):
    result = isolated_script(exported_package, """
from ribosome.application.process_parameters import generate_process_parameters

for dialect in ('rc4', 'rc5'):
    result = generate_process_parameters({
        'dialect': dialect,
        'steps': [{'kind': 'move', 'motion': 'MOVL', 'target': {'index': 1},
                   'speed': 50, 'acceleration': 80, 'blend': None}],
    })
    assert result.validation['valid']
    assert result.dialect == dialect
    assert result.code.startswith('NOP\\n') and result.code.endswith('\\nEND')
assert not any(name == 'llm_backend' or name.startswith('llm_backend.') for name in sys.modules)
print('clean export passed')
""")
    assert result.stdout.strip() == "clean export passed"


def test_tutorial_runs_all_code_cells_offline_from_a_fresh_export(exported_package):
    result = isolated_script(exported_package, """
import json

notebook = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
namespace = {'__name__': '__main__'}
for index, cell in enumerate(notebook['cells']):
    if cell['cell_type'] == 'code':
        source = ''.join(cell['source'])
        exec(compile(source, f'{sys.argv[2]}:cell-{index}', 'exec'), namespace)
assert not any(name == 'llm_backend' or name.startswith('llm_backend.') for name in sys.modules)
print('tutorial run-all passed')
""", TUTORIAL)
    assert result.stdout.strip().endswith("tutorial run-all passed")


def test_complete_codegen_notebook_records_candidate_parameters_offline(tmp_path):
    backend = ROOT.parents[1]
    if not (backend / "src/llm_backend/process_parameters.py").is_file():
        pytest.skip("complete code-generation example uses the backend checkout")
    script = """
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys

os.environ['IPYTHONDIR'] = sys.argv[2]
os.environ['LLM_BACKEND_ROOT'] = sys.argv[1]
os.environ['CODEGEN_OUTPUT_DIR'] = str(Path(sys.argv[2]) / 'artifacts')

def no_network(*args, **kwargs):
    raise AssertionError('Default notebook execution must remain offline')

socket.socket.connect = no_network
socket.create_connection = no_network
from IPython.core.interactiveshell import InteractiveShell

shell = InteractiveShell.instance()
notebook = json.loads((Path(sys.argv[1]) / 'vendors/ribosome/nbs/05.application.code_generation.ipynb').read_text())
for index, cell in enumerate(notebook['cells']):
    if cell['cell_type'] != 'code':
        continue
    source = ''.join(cell['source']).replace('WRITE_ARTIFACTS = True', 'WRITE_ARTIFACTS = False')
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        result = shell.run_cell(source, store_history=False)
    if result.error_before_exec or result.error_in_exec:
        raise AssertionError(f'Cell {index} failed: {output.getvalue()}')

assert shell.user_ns['RUN_LLM_GENERATION'] is False
assert shell.user_ns['RUN_LEAN_VALIDATION'] is False
assert shell.user_ns['WRITE_ARTIFACTS'] is False
assert shell.user_ns['artifact_paths'] == []
assert not (Path(sys.argv[2]) / 'artifacts').exists()
reports = shell.user_ns['validation_reports']
assert set(reports) == {'transfer_rc4', 'transfer_rc5', 'stud_weld_rc5'}
for name, report in reports.items():
    assert report['valid'], (name, report)
    assert report['parsed_parameters']['valid']
    assert report['parsed_parameters']['instructions']
    expected = shell.user_ns['examples'][name][1].parsed_parameters
    assert report['parsed_parameters'] == expected
print('complete code-generation notebook passed')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(backend), str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip().endswith("complete code-generation notebook passed")
