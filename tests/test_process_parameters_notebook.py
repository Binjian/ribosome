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
