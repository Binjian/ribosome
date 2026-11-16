"""Export notebook-authored modules for standalone or backend use, without execution."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

from nbdev.export import nb_export


PROFILES = ("standalone", "llm-backend")
BACKEND_MODULES = {
    "01.core.dom.embedding.natural_text.ipynb": "core.dom.embedding.natural_text",
    "01.core.dom.embedding.object.ipynb": "core.dom.embedding.object",
    "01.rag.object.ipynb": "rag.object",
    "05.application.process_parameters.ipynb": "application.process_parameters",
}
_DEFAULT_EXP = re.compile(r"^#\s*\|\s*default_exp\s+(\S+)\s*$", re.MULTILINE)
_MODULE_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def prepare(source=None, destination=None, *, profile=None):
    """Export a checkout's runtime modules; explicit profiles override the environment.

    Standalone exports every notebook with a default module. The llm-backend
    profile exports object RAG, object/natural-text embedding, and robot process
    parameter generation.
    Neither profile executes notebook code or reads application dotenv files.
    """
    if profile is None:
        profile = os.environ.get("RIBOSOME_PROFILE", "standalone")
    if profile not in PROFILES:
        raise ValueError(f"Unknown Ribosome profile {profile!r}; choose from {PROFILES}")
    source = Path(source).resolve() if source is not None else Path(__file__).resolve().parents[1]
    destination = Path(destination).resolve() if destination is not None else source / "ribosome"
    notebooks = source / "nbs"
    if not notebooks.is_dir():
        raise FileNotFoundError(f"Missing Ribosome notebooks: {notebooks}. Pass --source to a source checkout.")

    paths = ([notebooks / name for name in BACKEND_MODULES] if profile == "llm-backend"
             else sorted(notebooks.glob("*.ipynb")))
    modules = []
    # Validate the complete selection before writing any exports.
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"Missing Ribosome notebook: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        code = "\n".join("".join(cell.get("source", [])) for cell in data["cells"]
                         if cell["cell_type"] == "code")
        match = _DEFAULT_EXP.search(code)
        if match is None:
            if profile == "llm-backend":
                raise ValueError(f"Missing default_exp in {path}")
            continue
        module = match.group(1)
        if not _MODULE_NAME.fullmatch(module):
            raise ValueError(f"Invalid default_exp {module!r} in {path}")
        if profile == "llm-backend" and module != BACKEND_MODULES[path.name]:
            raise ValueError(f"Unexpected default_exp {module!r} in {path}")
        modules.append((path, destination.joinpath(*module.split(".")).with_suffix(".py")))
    if not modules:
        raise ValueError(f"No exportable Ribosome notebooks found in {notebooks}")

    for notebook, output in modules:
        output.parent.mkdir(parents=True, exist_ok=True)
        parent = output.parent
        while parent.is_relative_to(destination):
            initializer = parent / "__init__.py"
            if not initializer.exists():
                initializer.touch()
            if parent == destination:
                break
            parent = parent.parent
        try:
            nb_export(str(notebook), lib_path=str(destination))
            compile(output.read_text(encoding="utf-8"), str(output), "exec")
        except Exception as exc:
            raise RuntimeError(f"Failed to export {notebook}: {exc}") from exc
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, help="Default: RIBOSOME_PROFILE or standalone")
    parser.add_argument("--source", type=Path, help="Ribosome source checkout (defaults to this package's checkout)")
    parser.add_argument("--destination", type=Path, help="Output package directory (defaults to SOURCE/ribosome)")
    args = parser.parse_args(argv)
    try:
        destination = prepare(args.source, args.destination, profile=args.profile)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
    print(destination)


if __name__ == "__main__":
    main()
