"""Task-only RAG and evaluator integration against a synthetic SQLite catalog."""
import ast
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import nbformat
import pytest

from ribosome.rag.object import ObjectRAG, canonical
from utils.object_rag_evaluation import digest
from utils.task_rag import (TaskRAG, TaskEvaluationRun, TASK_FIELDS, rerank_tasks,
                            task_case_metrics, task_index_coverage, require_task_coverage)


def payload(**changes):
    result = {key: '' for key in TASK_FIELDS}
    result.update(supported=True, explanation='按规定启动作业。', procedure='准备后启动，再停止。',
                  manual='Fixture C-1, corpus 4.0', citations=['E1'])
    result.update(changes)
    return result


@pytest.fixture
def rag(tmp_path, monkeypatch):
    mapping = [dict(key='manual', profile=p, category='arc_welding', revision='C-1')
               for p in ('4.0', '5.0')]
    monkeypatch.setattr('ribosome.rag.object.load_mapping', lambda _: ({'arc_welding': '弧焊'}, mapping))
    config = dict(provider='ollama-pool', hosts=['fixture'])
    fingerprint = digest(dict(schema=1, model='fake-embed', config=config))
    connection = sqlite3.connect(tmp_path/'catalog.sqlite3')
    connection.executescript('''CREATE TABLE settings (key TEXT, value TEXT);
        CREATE TABLE active_documents (document_id TEXT, generation_id TEXT);
        CREATE TABLE objects (generation_id TEXT, record TEXT, vector_id TEXT);''')
    connection.executemany('INSERT INTO settings VALUES (?,?)', [
        ('identity', json.dumps(dict(fingerprint=fingerprint, collection_id='fixture'))),
        ('catalog_id', json.dumps('fixture-catalog')), ('dimension', '2')])
    vector_ids = []
    for profile in ('4.0', '5.0'):
        generation = 'g'+profile
        document_id = 'doc'+profile
        connection.execute('INSERT INTO active_documents VALUES (?,?)', (document_id, generation))
        base = dict(document_id=document_id, source_file=f'assets/code-docs.{profile}/.md/manual.md',
                    semantics_file=f'assets/code-docs.{profile}/.md/manual_tasks.json', attributes={},
                    children_ids=[], parent_id=None, section_headings=['3.1 编程'], summary='Stored summary')
        document = dict(base, id=document_id, type='document', content='{}')
        connection.execute('INSERT INTO objects VALUES (?,?,?)', (generation, json.dumps(document), None))
        for kind in ('task', 'table', 'table_row', 'table_row_group', 'table_cell'):
            record = dict(base, id=f'{kind}-{profile}', source_id=1, type=kind,
                          content='完整作业：准备、启动、停止。', parent_id=document_id,
                          attributes=dict(caption='基本作业流程', headers=['作业'], line_start=2, line_end=5))
            if kind == 'table_row': record['parent_id'] = f'table-{profile}'
            vector = record['id']
            vector_ids.append(vector)
            connection.execute('INSERT INTO objects VALUES (?,?,?)', (generation, json.dumps(record), vector))
    connection.commit(); connection.close()
    class Collection:
        id = 'fixture'
        metadata = dict(object_index_config=fingerprint, object_catalog_id='fixture-catalog',
                        **{'hnsw:space': 'cosine'})
        def __init__(self): self.calls = []
        def query(self, **kwargs):
            self.calls.append(kwargs)
            ids = sorted(kwargs['ids'])[:kwargs['n_results']]
            return dict(ids=[ids], distances=[[0.1]*len(ids)])
    class Embedder:
        def __init__(self): self.calls = []
        async def embed(self, **kwargs):
            self.calls.append(kwargs)
            return dict(embeddings=[[1., 0.]])
    class Generator:
        def __init__(self): self.calls = []; self.failure = None; self.response = payload()
        async def chat(self, **kwargs):
            self.calls.append(deepcopy(kwargs))
            assert 'PRIVATE_REVIEWER_CANARY' not in json.dumps(kwargs)
            if self.failure: raise self.failure
            return dict(message=dict(content=json.dumps(self.response)))
    app = TaskRAG(tmp_path, Collection(), Embedder(), Generator(), mapping_dir=tmp_path,
                  embed_model='fake-embed', embedding_config=config, rag_model='fake-rag')
    case = dict(id='case-4', category='arc_welding', kind='task', profile='4.0', revision='C-1',
                source_md='assets/code-docs.4.0/.md/manual.md', task_id=1, task_object_id='task-4.0',
                task_content_digest=digest('完整作业：准备、启动、停止。'), question='应如何准备并启动作业？',
                representation='task', heading_path=['3.1 编程'], anchors=['启动'],
                expected_facts=['PRIVATE_REVIEWER_CANARY'],
                references=[dict(excerpt='PRIVATE_REVIEWER_CANARY')])
    yield app, case, tmp_path
    app.close()


def test_task_suffix_and_only_task_candidates_with_complete_repeated_content(rag):
    app, case, _ = rag
    assert canonical('assets/code-docs.4.0/.md/manual_tasks.json') == ('manual', '4.0')
    result = asyncio.run(app.retrieve(case['question'], case['category']))
    assert len(result['hits']) == 2
    assert all(h['record']['type'] == 'task' for h in result['hits'])
    assert {v for call in app.collection.calls for v in call['ids']} == {'task-4.0', 'task-5.0'}
    assert result['context'].count('完整作业：准备、启动、停止。') == 2
    assert '基本作业流程' in result['context'] and '3.1 编程' in result['context']
    assert app.embedder.calls == [dict(model='fake-embed', input=case['question'])]
    records, _, _ = app.snapshot('arc_welding')
    assert not ObjectRAG._eligible_record(app, result['hits'][0]['record'], 'g4.0', records)
    assert ObjectRAG._eligible_record(app, records[('g4.0', 'table-4.0')][0], 'g4.0', records)
    assert ObjectRAG._eligible_record(app, records[('g4.0', 'table_row-4.0')][0], 'g4.0', records)


def test_budget_omissions_are_not_task_context_success(rag):
    app, case, _ = rag
    app.max_context_chars = 50
    evidence = asyncio.run(app.retrieve(case['question'], case['category']))
    metrics = task_case_metrics(case, {'retrieval': evidence}, {'manual'})
    assert metrics['expected_task_hit'] and not metrics['expected_task_in_context']
    assert evidence['omissions'] and not evidence['context']


@pytest.mark.parametrize('response', [payload(), payload(procedure='', parameters='速度参数为输入值。'),
                                     payload(procedure='', code='START', language='text'),
                                     payload(supported=False, procedure='', explanation='缺少参数范围。', citations=[])])
def test_task_answers_allow_procedures_parameters_optional_code_and_unsupported(rag, response):
    app, case, _ = rag
    app.generator.response = response
    result = asyncio.run(app.answer(case['question'], case['category']))
    assert response['explanation'] in result['answer']
    assert result['example_status'] == ('included' if response['supported'] else 'unsupported')
    assert 'instruction_statement' not in app.generator.calls[0]['format']['properties']
    assert 'Stored summary' in result['retrieved_summary']
    assert 'PRIVATE_REVIEWER_CANARY' not in json.dumps(app.generator.calls)


@pytest.mark.parametrize('changes', [dict(citations=['E999']), dict(procedure='', parameters='', code=''),
                                     dict(code='START', language='bad```'), dict(supported=False)])
def test_invalid_task_answers_retry_once_and_remain_retryable(rag, changes):
    app, case, _ = rag
    app.generator.response = payload(**changes)
    result = asyncio.run(app.answer(case['question'], case['category']))
    assert len(app.generator.calls) == 2 and result['example_status'] == 'unavailable'
    assert 'task answer' in result['answer']


def test_exact_task_metrics_distinguish_other_tasks_and_profiles(rag):
    app, case, _ = rag
    result = asyncio.run(app.retrieve(case['question'], case['category']))
    assert task_case_metrics(case, {'retrieval': result}, {'manual'})['expected_task_in_context']
    other = dict(case, task_object_id='task-5.0')
    assert not task_case_metrics(other, {'retrieval': result}, {'manual'})['expected_task_hit']
    other = dict(case, task_object_id='other-task')
    metrics = task_case_metrics(other, {'retrieval': result}, {'manual'})
    assert metrics['expected_manual_hit'] and not metrics['expected_task_hit']


def test_coverage_distinguishes_missing_document_task_vector_and_stale_content(rag):
    app, case, _ = rag
    rows = task_index_coverage(app, [case])
    assert rows[0]['status'] == 'ready'
    require_task_coverage(rows)
    for changes, expected in [(dict(source_md='assets/code-docs.4.0/.md/missing.md'), 'missing_document'),
                              (dict(task_object_id='absent'), 'missing_task'),
                              (dict(task_content_digest='stale'), 'stale_content')]:
        rows = task_index_coverage(app, [dict(case, **changes)])
        assert rows[0]['status'] == expected
        with pytest.raises(ValueError): require_task_coverage(rows)
    records, docs, coverage = app.snapshot('arc_welding', all_categories=True)
    records[('g4.0', 'task-4.0')] = (records[('g4.0', 'task-4.0')][0], None)
    app.snapshot = lambda *a, **k: (records, docs, coverage)
    assert task_index_coverage(app, [case])[0]['status'] == 'missing_vector'


def test_frozen_replay_resumption_metrics_and_identity(rag):
    app, case, directory = rag
    run = TaskEvaluationRun(app, [case], directory/'results')
    asyncio.run(run.run())
    asyncio.run(run.run())
    assert len(app.embedder.calls) == len(app.generator.calls) == 1
    assert run.load(case)['answer']['example_status'] == 'included'
    assert run.metrics().iloc[0].expected_task_in_context
    changed = run.for_top_k(3)
    assert isinstance(changed, TaskEvaluationRun) and changed.run_id != run.run_id
    assert 'task_prompt' in json.loads((run.directory/'run.json').read_text())['identity']
    app.connection.execute('SELECT count(*) FROM objects')
    with pytest.raises(sqlite3.OperationalError): app.connection.execute('CREATE TABLE forbidden(x)')


@pytest.mark.parametrize('error', [TimeoutError('fixture'), asyncio.CancelledError()])
def test_task_replay_errors_and_cancellation_preserve_retrieval(rag, error):
    app, case, directory = rag
    app.generator.failure = error
    run = TaskEvaluationRun(app, [case], directory/'results')
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError): asyncio.run(run.run_case(case))
    else:
        asyncio.run(run.run_case(case))
    assert run.load(case)['retrieval_status'] == 'complete'
    assert run.load(case)['answer_status'] == 'error'
    app.generator.failure = None
    asyncio.run(run.run_case(case))
    assert len(app.embedder.calls) == 1 and len(app.generator.calls) == 2
    assert run.load(case)['answer_status'] == 'complete'


def test_category_rerank_is_deterministic_and_does_not_mutate_hits():
    hits = [dict(distance=.1, manual=dict(category='other'), record=dict(id='other')),
            dict(distance=.2, manual=dict(category='arc_welding'), record=dict(id='task'))]
    before = deepcopy(hits)
    assert rerank_tasks(hits, 'arc_welding')[0]['record']['id'] == 'task'
    assert hits == before


@pytest.mark.parametrize('path', [Path('nbs/06.test.rag.object.question_with_tasks.ipynb'),
                                  Path('nbs/05.application.rag.task.ipynb')])
def test_new_notebooks_validate_and_compile(path):
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    for cell in notebook.cells:
        if cell.cell_type == 'code':
            compile(cell.source, str(path), 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            # Interactive runs may save valid execution counts and outputs.
    source = '\n'.join(c.source for c in notebook.cells)
    assert 'OLLAMA_RAG_HOST' in source and 'trust_env=OLLAMA_TRUST_ENV' in source


def test_task_answer_recovers_after_one_correction(rag):
    app, case, _ = rag
    calls = []
    async def recovering(**kwargs):
        calls.append(deepcopy(kwargs))
        answer = payload(citations=['invalid']) if len(calls) == 1 else payload(procedure='', parameters='速度为输入参数。')
        return dict(message=dict(content=json.dumps(answer)))
    app.generator.chat = recovering
    result = asyncio.run(app.answer(case['question'], case['category']))
    assert result['example_status'] == 'included' and len(calls) == 2
    assert 'If a task answer cannot' in calls[1]['messages'][-1]['content']


def test_notebook_configs_use_separate_deployment_hosts(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('PROJ_ROOT', str(root))
    monkeypatch.setenv('OLLAMA_HOST', 'http://wrong-host:11434')
    monkeypatch.delenv('TASK_RAG_INDEX', raising=False)
    for name in ('06.test.rag.object.question_with_tasks.ipynb', '05.application.rag.task.ipynb'):
        notebook = nbformat.read(root/'nbs'/name, as_version=4)
        config = next(c.source for c in notebook.cells if c.cell_type == 'code')
        namespace = {}
        exec(config, namespace)
        assert namespace['OLLAMA_EMBED_HOST'] == 'http://172.27.74.49:11434'
        assert namespace['OLLAMA_RAG_HOST'] == 'http://172.27.74.16:11434'
        assert namespace['OLLAMA_TRUST_ENV'] is False
        assert namespace['BANK_PATH'] == root/'db/task_rag_tests/question_with_tasks/bank.json'
        if 'INDEX_DIR' in namespace:
            assert namespace['COLLECTION_NAME'] == 'task_embeddings'
            assert namespace['HOSTS'] == ['http://172.27.74.49:11434']
            assert '172.27.74.49-11434' in str(namespace['INDEX_DIR'])
