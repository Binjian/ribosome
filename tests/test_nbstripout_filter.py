"""Git must remain usable while notebook files contain merge conflicts."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


FILTER = Path(__file__).resolve().parents[1] / 'utils' / 'notebooks' / 'nbstripout_filter.py'


class NotebookFilterTests(unittest.TestCase):
    def run_filter(self, data):
        return subprocess.run(
            [sys.executable, str(FILTER)], input=data, capture_output=True,
        )

    def test_preserves_unparseable_and_unsupported_input(self):
        for data in (
            b'', b'<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> branch\n',
            b'{"cells": [', b'\xff', b'{}', b'[]',
            b'{"nbformat": 5, "cells": []}',
        ):
            with self.subTest(data=data):
                result = self.run_filter(data)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, data)
                self.assertEqual(result.stderr, b'')

    @unittest.skipUnless(shutil.which('nbstripout'), 'requires nbstripout')
    def test_strips_supported_notebook_outputs(self):
        notebook = dict(nbformat=4, nbformat_minor=5, metadata={}, cells=[dict(
            id='example', cell_type='code', metadata={}, source=['print(1)'],
            execution_count=1,
            outputs=[dict(output_type='stream', name='stdout', text=['1\n'])],
        )])
        result = self.run_filter(json.dumps(notebook).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        cell = json.loads(result.stdout)['cells'][0]
        self.assertEqual(cell['outputs'], [])
        self.assertIsNone(cell['execution_count'])
        self.assertEqual(''.join(cell['source']), 'print(1)')
