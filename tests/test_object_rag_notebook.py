import pytest
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
            # Saved outputs are valid after interactive notebook use.

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


@pytest.mark.parametrize('supported', [True, False])
def test_content_summary_accompanies_supported_and_unsupported_answers(supported):
    import asyncio
    from ribosome.rag.object import ObjectRAG
    app = object.__new__(ObjectRAG)
    app.rag_model = 'fake'
    app.max_output_tokens = 128
    hit = dict(generation='g', record=dict(id='row', summary='Sets the user coordinate number.',
               source_file='manual.md', attributes=dict(line_start=10, line_end=12)),
               manual=dict(profile='5.0', revision='C-1'))
    async def retrieve(*args, **kwargs):
        app.last_result = dict(hits=[hit], context='[E1] Exact evidence')
        return app.last_result
    requests = []
    expected_answer = (
        'General operation, C-1 [E1]\n```text\nSET_FRAME <user_frame_number>\n```'
        if supported else 'Insufficient evidence for code.'
    )
    class Generator:
        async def chat(self, **kwargs):
            requests.append(kwargs)
            return {'message': {'content': expected_answer}}

    app.retrieve = retrieve
    app.generator = Generator()
    result = asyncio.run(app.answer('question', 'general_operation'))
    assert 'Sets the user coordinate number.' in result['retrieved_summary']
    assert 'manual.md' in result['retrieved_summary']
    assert 'C-1' in result['retrieved_summary']
    assert result['answer'] == expected_answer
    request = requests[0]
    system, user = request['messages']
    assert 'Selected category: general_operation' in user['content']
    assert 'Question: question' in user['content']
    assert '[E1] Exact evidence' in user['content']
    assert 'actual code statement' in system['content']
    assert 'Immediately before the final code block' in system['content']
    assert 'If the evidence does not support the requested instruction' in system['content']
    assert request['stream'] is False
    assert request['options']['num_predict'] == 128


def test_empty_evidence_still_returns_content_summary():
    import asyncio
    from ribosome.rag.object import ObjectRAG
    app = object.__new__(ObjectRAG)
    async def retrieve(*args, **kwargs):
        return dict(hits=[], context='')
    app.retrieve = retrieve
    result = asyncio.run(app.answer('question', 'general_operation'))
    assert 'No relevant indexed content' in result['retrieved_summary']
    assert 'No eligible evidence' in result['answer']
