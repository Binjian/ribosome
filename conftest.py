"""Prepare ignored notebook exports for tests without executing notebook cells."""
import json
from pathlib import Path
import re
import subprocess

from nbdev.export import nb_export


def pytest_sessionstart(session):
    root = Path(__file__).resolve().parent
    # Preserve checked-in modules and any local edits to them. Other modules are
    # intentionally gitignored and must be generated even on a fresh checkout.
    versioned = set(subprocess.run(
        ['git', 'ls-files', '--', 'ribosome'], cwd=root,
        check=True, capture_output=True, text=True,
    ).stdout.splitlines())
    for notebook in sorted((root / 'nbs').glob('*.ipynb')):
        data = json.loads(notebook.read_text())
        source = '\n'.join(''.join(cell.get('source', [])) for cell in data['cells']
                           if cell['cell_type'] == 'code')
        match = re.search(r'^#\s*\|\s*default_exp\s+(\S+)', source, re.MULTILINE)
        if match is None:
            continue
        relative = 'ribosome/' + match.group(1).replace('.', '/') + '.py'
        if relative not in versioned or not (root / relative).exists():
            nb_export(str(notebook), lib_path=str(root / 'ribosome'))
