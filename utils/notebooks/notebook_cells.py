"""Locate notebook code and headings without relying on cleaned cell IDs."""
import ast
from functools import lru_cache
import re


class _Definitions(ast.NodeVisitor):
    def __init__(self):
        self.names = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_FunctionDef(self, node):
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef


@lru_cache(maxsize=512)
def _definitions(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return frozenset()
    visitor = _Definitions()
    visitor.visit(tree)
    return frozenset(visitor.names)


def defined_names(cell):
    """Return bindings outside function/class bodies, without executing code."""
    return _definitions(cell.source) if cell.cell_type == 'code' else frozenset()


def find_cell(notebook, *, defines=(), heading=None, containing=None, required=True):
    """Require an unambiguous cell selected by its declarations or contents."""
    if isinstance(defines, str):
        defines = (defines,)
    if not (defines or heading or containing):
        raise ValueError('Specify declarations, a heading or source text')
    matches = [cell for cell in notebook.cells
               if (not defines or set(defines) <= defined_names(cell))
               and (heading is None or cell.cell_type == 'markdown'
                    and cell.source.splitlines()[:1] == [heading])
               and (containing is None or cell.cell_type == 'code'
                    and containing in cell.source)]
    if not matches and not required:
        return None
    if len(matches) != 1:
        raise ValueError(f'Expected one notebook cell for {defines or heading or containing!r}; '
                         f'found {len(matches)}')
    return matches[0]


def load_question_banks(notebook, namespace=None):
    """Execute only bank declarations and registration in notebook order."""
    namespace = {} if namespace is None else namespace
    banks = {'GENERAL_OPERATION_CASES', 'ARC_WELDING_CASES', 'SAMPLED_CASES'}
    for cell in notebook.cells:
        names = defined_names(cell)
        if banks & names or any(re.fullmatch(r'[A-Z][A-Z0-9_]*_SAMPLE_CASES', name)
                                for name in names):
            exec(cell.source, namespace)
    missing = banks - namespace.keys()
    if missing:
        raise ValueError(f'Missing notebook question banks: {sorted(missing)}')
    return namespace
