#!/usr/bin/env python3
"""Run nbstripout, preserving malformed or unsupported notebooks.

Use as Git's clean filter: python3 utils/nbstripout_filter.py
Requires the nbstripout executable on PATH. Malformed input (including merge
conflicts) and unsupported notebooks pass through unchanged so Git can still
inspect and save them without a format conversion.
"""

import json
import shutil
import subprocess
import sys


def main():
    data = sys.stdin.buffer.read()
    try:
        notebook = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        notebook = None
    version = notebook.get("nbformat") if isinstance(notebook, dict) else None
    if type(version) is not int or not 1 <= version <= 4:
        sys.stdout.buffer.write(data)
        return 0

    executable = shutil.which("nbstripout")
    if executable is None:
        print("Notebook clean filter requires nbstripout on PATH.", file=sys.stderr)
        return 1
    result = subprocess.run([executable], input=data, stdout=subprocess.PIPE)
    if result.returncode == 0:
        sys.stdout.buffer.write(result.stdout)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
