import ast
from pathlib import Path
import nbformat

NOTEBOOK = Path('nbs/05.application.rag.object.ipynb')

def namespace():
    nb = nbformat.read(NOTEBOOK, as_version=4)
    ns = {}
    exec(nb.cells[2].source, ns)
    from ribosome.rag.object import canonical, load_mapping
    ns.update(canonical=canonical, load_mapping=lambda: load_mapping(ns['PROJ_ROOT'] / 'assets/coding_categories'))
    return ns

def test_notebook_valid_and_compiles():
    nb = nbformat.read(NOTEBOOK, as_version=4)
    nbformat.validate(nb)
    assert 'design and plan' in nb.cells[0].source
    for cell in nb.cells:
        if cell.cell_type == 'code':
            compile(cell.source, str(NOTEBOOK), 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            assert not cell.outputs

def test_aliases_and_mapping():
    ns = namespace()
    canonical = ns['canonical']
    assert canonical('assets/code-docs.4.0/.md/manual_semantics.json') == ('manual','4.0')
    assert canonical('assets/md.json.semantics.coding.5.0/manual.md') == ('manual','5.0')
    categories, entries = ns['load_mapping']()
    assert len(categories) == 18
    assert {e['profile'] for e in entries} >= {'4.0','5.0'}

def test_real_catalog_read_only_coverage():
    import sqlite3
    ns = namespace()
    app = object.__new__(ns['ObjectRAG'])
    path = ns['INDEX_DIR'] / 'catalog.sqlite3'
    if not path.exists():
        import pytest
        pytest.skip('Example corpus unavailable')
    app.connection = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)
    app.categories, app.mapping = ns['load_mapping']()
    try:
        _, docs, coverage = app.snapshot('teaching_free_welding')
        assert docs
        assert all('revision' in d for d in docs.values())
        import pytest
        with pytest.raises(sqlite3.OperationalError):
            app.connection.execute('CREATE TABLE forbidden(x)')
    finally:
        app.close()

def test_reusable_notebook_exports_without_application_globals():
    import inspect
    from ribosome.rag.object import ObjectRAG
    core = nbformat.read('nbs/01.rag.object.ipynb', as_version=4)
    nbformat.validate(core)
    ns = {}
    for cell in core.cells:
        if cell.cell_type == 'code':
            compile(cell.source, '01.rag.object.ipynb', 'exec')
            exec(cell.source, ns)
    assert not {'PROJ_ROOT', 'HOSTS', 'RAG_MODEL', 'INDEX_DIR'} & ns.keys()
    parameters = inspect.signature(ObjectRAG).parameters
    assert parameters['mapping_dir'].kind == inspect.Parameter.KEYWORD_ONLY
    assert parameters['max_context_chars'].default == 24000
    assert parameters['max_output_tokens'].default == 2048
    application = nbformat.read(NOTEBOOK, as_version=4)
    assert all('class ObjectRAG' not in c.source for c in application.cells)
