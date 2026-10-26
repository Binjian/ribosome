import pytest
import json
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

def test_aliases_and_mapping(tmp_path):
    # Category loading is a unit test, not a dependency on a private corpus.
    import pandas as pd
    mapping = tmp_path / 'assets/coding_categories'
    mapping.mkdir(parents=True)
    (mapping / 'category_names.csv').write_text(
        'category,chinese_name\nvision,视觉\ngeneral_operation,通用操作\n', encoding='utf-8')
    pd.DataFrame([
        {'category': 'vision', 'path': 'assets/code-docs.4.0/.md/manual.md', 'version': 'A'},
        {'category': 'vision', 'path': 'assets/md.json.semantics.coding.5.0/manual.md', 'version': 'B'},
    ]).to_excel(mapping / 'coding_manuals_markdown.all.xlsx', index=False)
    ns = namespace()
    ns['PROJ_ROOT'] = tmp_path
    canonical = ns['canonical']
    assert canonical('assets/code-docs.4.0/.md/manual_semantics.json') == ('manual','4.0')
    assert canonical('assets/md.json.semantics.coding.5.0/manual.md') == ('manual','5.0')
    categories, entries = ns['load_mapping']()
    assert categories == {"vision": "视觉", "general_operation": "通用操作"}
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
    payload = example_payload(supported=supported)
    expected_answer = payload['explanation']
    class Generator:
        async def chat(self, **kwargs):
            requests.append(kwargs)
            return {'message': {'content': json.dumps(payload)}}

    app.retrieve = retrieve
    app.generator = Generator()
    result = asyncio.run(app.answer('question', 'general_operation'))
    assert 'Sets the user coordinate number.' in result['retrieved_summary']
    assert 'manual.md' in result['retrieved_summary']
    assert 'C-1' in result['retrieved_summary']
    assert expected_answer in result['answer']
    if supported:
        assert result['example_status'] == 'included'
        assert 'Environment and prerequisites' in result['answer']
        assert result['answer'].endswith('```text\n' + payload['code'] + '\n```')
    else:
        assert result['answer'] == expected_answer
        assert result['example_status'] == 'unsupported'
    request = requests[0]
    system, user = request['messages']
    assert 'Selected category: general_operation' in user['content']
    assert 'Question: question' in user['content']
    assert '[E1] Exact evidence' in user['content']
    assert 'actual code statement' in system['content']
    assert 'follow-up' in system['content']
    assert request['format']['properties']['supported'] == {'type': 'boolean'}
    assert 'instruction_statement' in request['format']['required']
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


def example_payload(supported=True):
    # Synthetic manual syntax; these are test fixtures, not controller examples.
    return dict(
        supported=supported,
        explanation='Assign the prepared value.' if supported else 'Missing documented assignment syntax.',
        scenario='Copy an input into the working variable.',
        environment='Synthetic manual: source and result are integer variables.',
        expected_result='result holds the copied value.',
        code='SET source, 5\nSET target, source\nSET result, target' if supported else '',
        instruction_statement='SET target, source' if supported else '',
        language='text', manual='Synthetic C-1, corpus 5.0', citations=['E1'],
    )


@pytest.mark.parametrize('invalid', ['single_line', 'no_fence_fields', 'unknown_citation'])
@pytest.mark.parametrize('recovers', [True, False])
def test_contextual_example_validation_retries_and_rejects_incomplete_examples(invalid, recovers):
    import asyncio
    from ribosome.rag.object import ObjectRAG
    app = object.__new__(ObjectRAG)
    app.rag_model, app.max_output_tokens = 'fake', 2048
    calls = []
    bad = example_payload()
    if invalid == 'single_line':
        bad['code'] = bad['instruction_statement']
    elif invalid == 'no_fence_fields':
        bad.pop('environment')
    else:
        bad['citations'] = ['E999']
    async def retrieve(*args, **kwargs):
        app.last_result = dict(hits=[], context='[E1] Synthetic manual evidence')
        return app.last_result
    class Generator:
        async def chat(self, **kwargs):
            calls.append(kwargs)
            payload = example_payload() if recovers and len(calls) == 2 else bad
            return {'message': {'content': json.dumps(payload)}}
    app.retrieve, app.generator = retrieve, Generator()
    result = asyncio.run(app.answer('Copy source to target', 'general_operation'))
    assert len(calls) == 2
    assert len(calls[0]['messages']) == 2
    assert len(calls[1]['messages']) == 4
    assert 'retrieved_summary' in result
    if recovers:
        assert result['example_status'] == 'included'
        assert result['answer'].endswith('```')
        assert 'SET target, source' in result['answer']
    else:
        assert result['example_status'] == 'unavailable'
        assert 'Validation issue:' in result['answer']
        assert '```' not in result['answer']


def test_row_evidence_includes_surrounding_table_syntax():
    import asyncio
    from ribosome.rag.object import ObjectRAG
    app = object.__new__(ObjectRAG)
    app.embed_model, app.dimension, app.max_context_chars = 'fake', 2, 24000
    table = dict(id='table', document_id='doc', type='table', parent_id=None,
                 content='Function: assignment. Syntax: SET target, source. Example: SET I1, 5.',
                 attributes=dict(line_start=1, line_end=20))
    row = dict(id='row', document_id='doc', type='table_row', parent_id='table',
               content='Function: assignment.', source_file='synthetic.md',
               children_ids=[], attributes=dict(line_start=2, line_end=3))
    records = {('g','table'): (table,None), ('g','row'): (row,'vector')}
    app.snapshot = lambda category: (records, {('g','doc'): dict(profile='5.0',revision='C-1')}, {})
    class Embedder:
        async def embed(self, **kwargs): return dict(embeddings=[[1,0]])
    class Collection:
        def query(self, **kwargs): return dict(ids=[['vector']], distances=[[0.1]])
    app.embedder, app.collection = Embedder(), Collection()
    result = asyncio.run(app.retrieve('assignment', 'general_operation'))
    assert 'Syntax: SET target, source' in result['context']
    assert 'Parent table context, source lines 1-20' in result['context']
