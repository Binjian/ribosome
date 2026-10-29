"""Offline checks for source sampling, persisted stages and evaluation metrics."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import nbformat
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'utils'))
from object_rag_evaluation import (
    EvaluationRun, QueryEmbeddingCache, atomic_json, case_metrics, csv_rows, expected_hit,
    plain, read_json, sample_units, source_key, source_units, compatible_review, review_input, review_text, digest, cache_readonly_snapshots,
)


@pytest.fixture
def experiment(tmp_path):
    case = dict(id='sample_test_01', category='test', kind='procedure', profile='', revision='V1',
                source_md='assets/code-docs/.md/V3/manual.md', section='2',
                heading_path=['2 Procedure'], question='如何准备工具？', anchors=['TOOL'],
                expected_facts=['PRIVATE_REFERENCE_CANARY'], references=[dict(markdown='manual.md',
                line_start=1, line_end=2, excerpt='PRIVATE_REFERENCE_CANARY')])
    hit = dict(generation='g', distance=.1, manual=dict(key='V3/manual', profile='4.0', revision='V1'),
               record=dict(id='row', type='table_row', source_file='assets/code-docs.4.0/.md/V3/manual.md',
                           content='TOOL documented evidence', summary='Stored summary',
                           attributes=dict(line_start=1, line_end=2), section_headings=['2 Procedure']))
    evidence = dict(question=case['question'], category='test', hits=[hit],
                    context='[E1] manual | '+hit['record']['source_file']+' | lines 1-2 | row\nTOOL documented evidence',
                    coverage=dict(indexed=[hit['manual']], missing=[]), omissions=[])
    connection = sqlite3.connect(':memory:')
    connection.executescript('CREATE TABLE active_documents (document_id TEXT, generation_id TEXT);'
                             'CREATE TABLE settings (key TEXT, value TEXT);')
    connection.execute('INSERT INTO active_documents VALUES (?,?)', ('doc', 'g'))
    counts = dict(retrieval=0, generation=0)

    async def retrieve(*args, **kwargs):
        counts['retrieval'] += 1
        return deepcopy(evidence)

    class Generator:
        failure = None

        async def chat(self, **kwargs):
            counts['generation'] += 1
            assert 'PRIVATE_REFERENCE_CANARY' not in json.dumps(kwargs)
            assert 'TOOL documented evidence' in kwargs['messages'][-1]['content']
            if self.failure:
                raise self.failure
            return dict(message=dict(content=json.dumps(dict(
                supported=False, explanation='Procedure described, no code example needed.',
                scenario='', environment='', expected_result='', code='', instruction_statement='',
                language='', manual='', citations=[]))))

    app = SimpleNamespace(connection=connection, categories={'test': 'Test'}, rag_model='fake',
        embed_model='fake-embed', embedding_config={}, max_context_chars=24000, max_output_tokens=2048,
        snapshot=lambda *a, **k: ({}, {'doc': hit['manual']}, {}), retrieve=retrieve, generator=Generator())
    yield SimpleNamespace(case=case, evidence=evidence, app=app, counts=counts, directory=tmp_path)
    connection.close()


def test_replay_persistence_and_case_config_index_isolation(experiment):
    x = experiment
    run = EvaluationRun(x.app, [x.case], x.directory)
    asyncio.run(run.run())
    asyncio.run(run.run())
    assert x.counts == dict(retrieval=1, generation=1)
    assert read_json(run.case_bank_path)['cases'] == [x.case]
    state = run.load(x.case)
    assert state['answer_status'] == 'complete'
    assert 'Stored summary' in state['answer']['retrieved_summary']
    assert state['answer']['example_status'] == 'unsupported'
    assert run.metrics().iloc[0].expected_source_in_context
    changed = deepcopy(x.case)
    changed['question'] += '补充'
    assert run.path(changed) != run.path(x.case)
    assert run.for_top_k(3).directory != run.directory
    x.app.mapping = [dict(key='changed', profile='4.0', category='test', revision='V2')]
    assert EvaluationRun(x.app, [x.case], x.directory).directory != run.directory
    del x.app.mapping
    x.app.rag_model = 'other-model'
    assert EvaluationRun(x.app, [x.case], x.directory).directory != run.directory
    x.app.rag_model = 'fake'
    x.app.connection.execute('UPDATE active_documents SET generation_id="new"')
    assert EvaluationRun(x.app, [x.case], x.directory).directory != run.directory


@pytest.mark.parametrize('error', [RuntimeError('generation failed'), asyncio.CancelledError()])
def test_retry_and_cancellation_keep_successful_retrieval(experiment, error):
    x = experiment
    run = EvaluationRun(x.app, [x.case], x.directory)
    x.app.generator.failure = error
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run.run_case(x.case))
    else:
        asyncio.run(run.run_case(x.case))
    state = run.load(x.case)
    assert state['retrieval_status'] == 'complete'
    assert state['answer_status'] == 'error'
    assert 'Stored summary' in state['partial_answer']['retrieved_summary']
    x.app.generator.failure = None
    asyncio.run(run.run_case(x.case))
    assert x.counts == dict(retrieval=1, generation=2)
    assert run.load(x.case)['answer_error'] == ''


def test_atomic_publish_failure_preserves_previous_output(tmp_path, monkeypatch):
    path = tmp_path/'state.json'
    atomic_json(path, {'completed': 1})

    def fail(*args):
        raise OSError('disk failure')

    monkeypatch.setattr('object_rag_evaluation.os.replace', fail)
    with pytest.raises(OSError):
        atomic_json(path, {'completed': 2})
    assert read_json(path) == {'completed': 1}
    assert list(tmp_path.iterdir()) == [path]


def test_invalid_generation_is_retryable_without_retrieving_again(experiment):
    x = experiment
    original = x.app.generator.chat

    async def malformed(**kwargs):
        return dict(message=dict(content='{}'))

    x.app.generator.chat = malformed
    run = EvaluationRun(x.app, [x.case], x.directory)
    asyncio.run(run.run_case(x.case))
    failed = run.load(x.case)
    assert failed['answer_status'] == 'error'
    assert failed['answer']['example_status'] == 'unavailable'
    assert 'Stored summary' in failed['answer']['retrieved_summary']
    x.app.generator.chat = original
    asyncio.run(run.run_case(x.case))
    assert run.load(x.case)['answer_status'] == 'complete'
    assert x.counts['retrieval'] == 1


def test_review_validation_requires_real_answer_claims_and_source_quotes():
    from review_object_rag_evaluation import validate_review
    data = dict(answer='The documented parameter is TOOL.', references=['TOOL is documented.'], supplied_evidence='')
    review = dict(assessment='correct', completeness='complete', citation_support='supported',
                  revision_fidelity='faithful', appropriate_abstention=False, unsupported_claims=[],
                  evidence_quote='TOOL is documented.', rationale='Matches the exact source.')
    validate_review(review, data)
    with pytest.raises(AssertionError, match='evidence_quote'):
        validate_review({**review, 'evidence_quote': 'Invented source text'}, data)
    with pytest.raises(AssertionError, match='unsupported_claims'):
        validate_review({**review, 'unsupported_claims': ['A claim the answer never made']}, data)


def test_batched_query_vectors_preserve_order_and_model_settings(tmp_path):
    calls = []

    class Embedder:
        async def embed(self, *, model, input):
            calls.append((model, input))
            values = [input] if isinstance(input, str) else input
            return dict(embeddings=[[float(len(q)), 1.] for q in values])

    cache = QueryEmbeddingCache(Embedder(), 'model', {'hosts': ['one']}, 2, tmp_path)

    async def run():
        await cache.prepare(['long', 'x', 'long'])
        assert (await cache.embed(model='model', input='long'))['embeddings'] == [[4., 1.]]
        assert (await cache.embed(model='model', input='x'))['embeddings'] == [[1., 1.]]

    asyncio.run(run())
    assert calls == [('model', ['long', 'x'])]
    other = QueryEmbeddingCache(Embedder(), 'model', {'hosts': ['two']}, 2, tmp_path)
    assert other.identity != cache.identity and other.load('long') is None
    with pytest.raises(ValueError, match='configured model'):
        asyncio.run(cache.embed(model='other', input='long'))
    atomic_json(cache.path('long'), dict(identity=cache.identity, question='long', embedding=[0., 0.]))
    with pytest.raises(ValueError, match='Invalid cached'):
        cache.load('long')


def test_answer_retry_invalidates_only_its_old_review(experiment):
    x = experiment
    state = dict(answer=dict(answer='Old fragment', example_status='unsupported'), retrieval=x.evidence)
    state['review'] = dict(answer_digest=digest(review_input(x.case, state)), assessment='partial')
    assert compatible_review(x.case, state)['assessment'] == 'partial'
    state['answer']['answer'] = 'A different completed answer'
    assert compatible_review(x.case, state) == {}


def test_review_view_keeps_literal_command_placeholders_and_order():
    rendered = review_text('<table><tr><td>WAIT &lt;参数1&gt;</td><td>IN#1=ON</td></tr>'
                           '<tr><td>OUT <IO号>=ON</td></tr></table>')
    assert 'WAIT <参数1>' in rendered and 'OUT <IO号>=ON' in rendered
    assert rendered.index('WAIT') < rendered.index('OUT')
    assert '<table>' not in rendered


def test_qualitative_review_spans_every_source_and_preserves_legacy_coverage():
    from collections import Counter
    from run_object_rag_evaluation import namespace
    from review_object_rag_evaluation import select_review_cases
    _, ns = namespace()
    legacy = ns['GENERAL_OPERATION_CASES'] + ns['ARC_WELDING_CASES']
    cases = legacy + ns['SAMPLED_CASES']
    chosen = select_review_cases(cases)
    assert len(chosen) == len({c['id'] for c in chosen}) == 182
    assert {c['id'] for c in legacy} <= {c['id'] for c in chosen}
    assert Counter(c['source_md'] for c in chosen if c['id'].startswith('sample_')) == {
        r['source_md']: 3 for r in ns['SAMPLING_MANIFEST']}


def test_legacy_docx_references_match_converted_markdown_directory_layout():
    from run_object_rag_evaluation import namespace
    from ribosome.rag.object import canonical
    _, ns = namespace()
    for case in ns['GENERAL_OPERATION_CASES']+ns['ARC_WELDING_CASES']:
        stem = Path(case['references'][0]['docx']).stem
        sources = {c['source_md'] for c in ns['SAMPLED_CASES'] if Path(c['source_md']).stem == stem}
        assert len(sources) == 1
        expected = canonical(sources.pop())[0]
        assert source_key(case) == expected
        assert expected_hit(case, dict(manual=dict(key=expected, profile=case['profile'])))
        assert not expected_hit(case, dict(manual=dict(key=expected, profile='wrong')))


def test_mismatched_checkpoint_is_not_reused_or_overwritten(experiment):
    x = experiment
    run = EvaluationRun(x.app, [x.case], x.directory)
    atomic_json(run.path(x.case), dict(case_digest='wrong', run_id=run.run_id, answer_status='complete'))
    before = run.path(x.case).read_bytes()
    with pytest.raises(ValueError, match='identity mismatch'):
        asyncio.run(run.run_case(x.case))
    assert run.path(x.case).read_bytes() == before
    assert x.counts == dict(retrieval=0, generation=0)


def test_snapshot_cache_keeps_views_separate_and_rejects_changed_generations(experiment):
    x = experiment
    original = x.app.snapshot
    calls = []

    def snapshot(category, **options):
        calls.append((category, options))
        records, docs, _ = original(category, **options)
        return records, docs, dict(category=category)

    x.app.snapshot = snapshot
    cache_readonly_snapshots(x.app)
    first = x.app.snapshot('test', all_categories=True)
    assert x.app.snapshot('test', all_categories=True) is first
    other = x.app.snapshot('other', all_categories=True)
    assert other[2]['category'] == 'other' and first[2]['category'] == 'test'
    assert len(calls) == 2
    x.app.connection.execute('UPDATE active_documents SET generation_id="changed"')
    with pytest.raises(ValueError, match='Active index changed'):
        x.app.snapshot('test', all_categories=True)


def test_source_identity_context_omission_and_heading_renumbering(experiment):
    x = experiment
    state = dict(retrieval=x.evidence)
    metrics = case_metrics(x.case, state, {'V3/manual'})
    assert metrics['corpus_available'] and metrics['expected_manual_hit']
    assert metrics['expected_source_in_context'] and metrics['anchors_in_context']
    # A sibling revision with the same filename is not an expected-source match.
    wrong = deepcopy(x.evidence['hits'][0])
    wrong['manual']['key'] = 'V4/manual'
    assert not expected_hit(x.case, wrong)
    x.evidence['context'] = ''
    metrics = case_metrics(x.case, state, set())
    assert not metrics['corpus_available']
    assert metrics['expected_manual_hit'] and not metrics['expected_source_in_context']
    assert not metrics['anchors_in_context']
    x.evidence['hits'][0]['record']['section_headings'] = ['7 Procedure']
    assert case_metrics(x.case, state, set())['expected_section_hit']


def test_bare_citation_headers_are_not_supplied_source_evidence(experiment):
    x = experiment
    header = x.evidence['context'].split('\n')[0]
    for body in ['', '\n', '\n[]']:
        result = deepcopy(x.evidence)
        result['context'] = header+body
        metrics = case_metrics(x.case, dict(retrieval=result), {'V3/manual'})
        assert metrics['expected_manual_hit']
        assert not metrics['expected_source_in_context']
        assert not metrics['anchors_in_context']


def test_sampling_is_reproducible_and_excludes_frontmatter():
    if not Path('assets/code-docs/product_manuals_markdown.csv').exists():
        pytest.skip('Source manifest is not installed')
    row = next(r for r in csv_rows('.') if r['category'] == 'load_analysis')
    if not Path(row['path']).exists():
        pytest.skip('Source manuals are not installed')
    seed, population, units = sample_units(row['path'], row['path'])
    assert (seed, population, units) == sample_units(row['path'], row['path'])
    assert len(units) == 20 and population == 16
    lines = Path(row['path']).read_text(encoding='utf-8-sig').splitlines()
    for u in units:
        assert u['excerpt'] == '\n'.join(lines[u['line_start']-1:u['line_end']])
        assert all('修订' not in t and '目录' not in t for t in u['heading_path'])


def test_new_notebook_compiles_and_relocation_is_complete():
    import ast
    application = nbformat.read('nbs/05.application.rag.object.ipynb', as_version=4)
    notebook = nbformat.read('nbs/06.test.rag.object.ipynb', as_version=4)
    nbformat.validate(application)
    nbformat.validate(notebook)
    assert not any(c.id in ['general-operation-tests', 'arc-welding-tests'] for c in application.cells)
    assert any('06.test.rag.object.ipynb' in c.source for c in application.cells)
    for cell in notebook.cells:
        if cell.cell_type == 'code':
            compile(cell.source, '06.test.rag.object.ipynb', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def test_all_760_cases_have_exact_source_references_and_distinct_questions():
    from run_object_rag_evaluation import namespace
    _, ns = namespace()
    sampled = ns['SAMPLED_CASES']
    legacy = ns['GENERAL_OPERATION_CASES'] + ns['ARC_WELDING_CASES']
    assert len(sampled) == 760 and len(legacy) == 68
    assert len({c['id'] for c in sampled+legacy}) == 828
    from collections import Counter
    assert Counter(c['source_md'] for c in sampled) == {r['source_md']: 20 for r in ns['SAMPLING_MANIFEST']}
    if Path('assets/code-docs/product_manuals_markdown.csv').exists():
        assert Counter(c['source_md'] for c in sampled) == {r['path']: 20 for r in csv_rows('.')}
    assert len({c['category'] for c in sampled}) == 18
    assert len({c['question'] for c in sampled}) == 760
    if not all(Path(c['source_md']).exists() for c in sampled):
        pytest.skip('Source Markdown manuals are not installed')
    sources = {}
    for case in sampled:
        if case['source_md'] not in sources:
            path = Path(case['source_md'])
            sources[case['source_md']] = (path.read_text(encoding='utf-8-sig').splitlines(),
                                         hashlib.sha256(path.read_bytes()).hexdigest())
        lines, signature = sources[case['source_md']]
        for ref in case['references']:
            assert ref['sha256'] == signature
            assert ref['excerpt'] == '\n'.join(lines[ref['line_start']-1:ref['line_end']])
            assert all(fact in plain(ref['excerpt']) for fact in case['expected_facts'])
            assert all(anchor in plain(ref['excerpt']) for anchor in case['anchors'])
    manifest = ns['SAMPLING_MANIFEST']
    assert len(manifest) == 38
    for entry in manifest:
        cases = [c for c in sampled if c['source_md'] == entry['source_md']]
        assert entry['selected_lines'] == [(c['references'][0]['line_start'],
                                            c['references'][0]['line_end']) for c in cases]


def test_rerunning_a_family_bank_does_not_duplicate_cases_or_manifest():
    from run_object_rag_evaluation import namespace
    notebook, ns = namespace()
    before_ids = [c['id'] for c in ns['SAMPLED_CASES']]
    before_manifest = deepcopy(ns['SAMPLING_MANIFEST'])
    bank = next(c for c in notebook.cells if c.id == 'sampled-general_operation-bank')
    exec(bank.source, ns)
    assert [c['id'] for c in ns['SAMPLED_CASES']] == before_ids
    assert ns['SAMPLING_MANIFEST'] == before_manifest
