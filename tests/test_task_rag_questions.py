"""Fixture-only task selection, authoring and bank publication checks."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd
import pytest

from utils.object_rag_evaluation import atomic_json, digest, read_json
from utils import task_rag_questions as questions


@pytest.fixture
def corpus(tmp_path):
    def document(profile, name='manual', *, count=2):
        directory = tmp_path / f'assets/code-docs.{profile}/.md'
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / f'{name}.md'
        source.write_text('# 流程\n启动和停止\n')
        tasks = [dict(type='task', id=i, content=f'<table><tr><td>作业</td><td>启动{i}并停止</td></tr></table>',
                      summary='PRIVATE_SPECULATIVE_SUMMARY', source_file=str(source),
                      caption='基本作业流程', headers=[], line_start=2, line_end=3,
                      children=[dict(type='table_row', id=100+i, content='later row', summary='row')])
                 for i in range(count)]
        tree = dict(file_path=str(source), summary='document', sections=[dict(
            t='Section', c=[dict(t='Header', c=[2, ['', [], []], [dict(t='Str', c='3.1 编程')]]),
                            tasks])], mirrors=deepcopy(tasks))
        path = directory/f'{name}_tasks.json'
        path.write_text(json.dumps(tree, ensure_ascii=False))
        return path
    def workbook(rows):
        path = tmp_path/'manuals.xlsx'
        with pd.ExcelWriter(path) as writer:
            for sheet, values in rows.items():
                pd.DataFrame(values, columns=['category', 'path', 'task_count', 'version']).to_excel(
                    writer, sheet_name=sheet, index=False)
        return path
    for profile in ('4.0', '5.0'):
        (tmp_path/f'assets/code-docs.{profile}/.md').mkdir(parents=True)
    return tmp_path, document, workbook


def row(profile='', name='manual', count=2, category='arc_welding'):
    prefix = f'assets/md.json.semantics.coding.{profile}' if profile else 'assets/code-docs/.md'
    return dict(category=category, path=f'{prefix}/{name}.md', task_count=count, version='C-1')


def selected(corpus):
    root, document, workbook = corpus
    document('4.0')
    path = workbook({'V4': [row('4.0')]})
    selections = questions.select_task_sources(root, path)
    cases, docs = questions.enumerate_task_cases(root, selections)
    return root, path, cases, docs


def test_sheet_precedence_aliases_and_mirrors(corpus):
    root, document, workbook = corpus
    document('4.0'); document('5.0'); document('4.0', 'fallback'); document('5.0', 'fallback')
    path = workbook({'all': [row(count=999), row(name='fallback')],
                     'V4': [row('4.0'), row('4.0')], 'V5': [row('5.0', count=0)]})
    selections = questions.select_task_sources(root, path)
    assert [(s['key'], s['profile']) for s in selections] == [
        ('fallback', '4.0'), ('manual', '4.0'), ('fallback', '5.0')]
    cases, docs = questions.enumerate_task_cases(root, selections)
    assert len(cases) == len({case['id'] for case in cases}) == 6
    assert all(case['representation'] == 'task' and case['references'] for case in cases)
    assert sum(d['actual_count'] for d in docs) == 6
    assert all(d['count_matches'] for d in docs)
    assert len(next(s for s in selections if s['key'] == 'manual')['origins']) == 2
    assert all('PRIVATE_SPECULATIVE_SUMMARY' not in json.dumps(c['authoring_input']) for c in cases)


def test_separate_profiles_and_count_discrepancy(corpus):
    root, document, workbook = corpus
    document('4.0'); document('5.0')
    path = workbook({'V4': [row('4.0', count=1)], 'V5': [row('5.0')]})
    cases, docs = questions.enumerate_task_cases(root, questions.select_task_sources(root, path))
    assert len(cases) == 4
    assert cases[0]['task_id'] == cases[2]['task_id']
    assert cases[0]['id'] != cases[2]['id']
    assert cases[0]['task_object_id'] != cases[2]['task_object_id']
    assert [d['count_matches'] for d in docs] == [False, True]


@pytest.mark.parametrize('error', ['missing', 'malformed', 'empty', 'bad_count', 'conflict'])
def test_invalid_selection_and_sources(corpus, error):
    root, document, workbook = corpus
    source = document('4.0', count=0 if error == 'empty' else 2)
    rows = [row('4.0', count=-1 if error == 'bad_count' else 2)]
    if error == 'conflict': rows.append(row('4.0', count=3))
    if error == 'missing': source.unlink()
    if error == 'malformed': source.write_text('{bad')
    path = workbook({'V4': rows})
    with pytest.raises((ValueError, FileNotFoundError)):
        questions.enumerate_task_cases(root, questions.select_task_sources(root, path))


class AuthorClient:
    def __init__(self): self.calls = []; self.mode = 'valid'
    async def chat(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        assert 'PRIVATE_SPECULATIVE_SUMMARY' not in json.dumps(kwargs)
        if self.mode == 'cancel': raise asyncio.CancelledError()
        if self.mode == 'network': raise TimeoutError('fixture timeout')
        payload = dict(question='启动作业时应如何执行启动并停止的流程？', evidence_quotes=['作业'])
        if self.mode == 'bad': payload['evidence_quotes'] = ['not in source']
        if self.mode == 'recover' and len(self.calls) == 1: return dict(message=dict(content='{}'))
        return dict(message=dict(content=json.dumps(payload, ensure_ascii=False)))


def test_author_validation_retry_cache_identity_and_bank(corpus):
    root, workbook, cases, docs = selected(corpus)
    original = deepcopy(cases)
    client = AuthorClient(); client.mode = 'recover'
    author = questions.TaskQuestionAuthor(client, 'fake', 'http://fixture', root/'authored')
    bank_path = root/'bank.json'
    report = asyncio.run(author.publish(cases, docs, bank_path, selection={'fixture': True}))
    assert report['published'] and len(client.calls) == 3
    assert cases == original
    bank = questions.load_task_bank(bank_path, root)
    assert bank['task_count'] == len(cases) == 2
    assert all('authoring_input' not in case for case in bank['cases'])
    before = bank_path.stat().st_mtime_ns
    asyncio.run(author.publish(cases, docs, bank_path, selection={'fixture': True}))
    assert len(client.calls) == 3 and bank_path.stat().st_mtime_ns == before
    assert author.checkpoint(cases[0]) != author.checkpoint(dict(cases[0], authoring_input={'changed': True}))
    changed = questions.TaskQuestionAuthor(client, 'other', 'http://fixture', root/'authored')
    assert changed.directory != author.directory
    changed = questions.TaskQuestionAuthor(client, 'fake', 'http://other', root/'authored')
    assert changed.directory != author.directory
    # A summary-only source edit changes source validation, but not authoring input/cache.
    source = root/cases[0]['tasks_json']; source.write_text(source.read_text()+'\n')
    with pytest.raises(ValueError, match='source changed'): questions.load_task_bank(bank_path, root)


@pytest.mark.parametrize('mode', ['bad', 'network', 'cancel'])
def test_failure_and_cancellation_preserve_previous_bank_and_retry(corpus, mode):
    root, workbook, cases, docs = selected(corpus)
    client = AuthorClient(); client.mode = mode
    author = questions.TaskQuestionAuthor(client, 'fake', 'fixture', root/'authored')
    destination = root/'bank.json'; atomic_json(destination, {'previous': True})
    if mode == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(author.publish(cases, docs, destination, selection={}))
    else:
        report = asyncio.run(author.publish(cases, docs, destination, selection={}))
        assert not report['published'] and len(report['failures']) == len(cases)
    assert read_json(destination) == {'previous': True}
    client.mode = 'valid'
    assert asyncio.run(author.publish(cases, docs, destination, selection={}))['published']


def test_bank_atomic_write_failure_preserves_output(corpus, monkeypatch):
    root, workbook, cases, docs = selected(corpus)
    author = questions.TaskQuestionAuthor(AuthorClient(), 'fake', 'fixture', root/'authored')
    destination = root/'bank.json'; atomic_json(destination, {'previous': True})
    original = questions.atomic_json
    def fail(path, value):
        if Path(path) == destination: raise OSError('fixture publication failure')
        return original(path, value)
    monkeypatch.setattr(questions, 'atomic_json', fail)
    with pytest.raises(OSError): asyncio.run(author.publish(cases, docs, destination, selection={}))
    assert read_json(destination) == {'previous': True}


@pytest.mark.parametrize('payload', [
    dict(question='', evidence_quotes=['作业']),
    dict(question='English only?', evidence_quotes=['作业']),
    dict(question='根据手册第3.1章节，如何启动？', evidence_quotes=['作业']),
    dict(question='如何启动？如何停止？', evidence_quotes=['作业']),
    dict(question='如何启动？', evidence_quotes=['invented']),
])
def test_reject_invalid_questions(payload):
    with pytest.raises(ValueError): questions.validate_question(payload, {'table_text': '作业 启动'})


def test_selection_controls(corpus):
    _, _, cases, _ = selected(corpus)
    assert questions.select_cases(cases) == cases
    assert questions.select_cases(cases, [cases[1]['id']]) == [cases[1]]
    for selection in ([], ['unknown']):
        with pytest.raises(ValueError): questions.select_cases(cases, selection)


def test_corrupt_author_checkpoint_is_repaired_and_summary_changes_reuse_questions(corpus):
    root, workbook, cases, docs = selected(corpus)
    client = AuthorClient()
    author = questions.TaskQuestionAuthor(client, 'fake', 'fixture', root/'authored')
    asyncio.run(author.publish(cases, docs, root/'bank.json', selection={}))
    assert len(client.calls) == 2
    # Refresh source references after a summary-only change, without asking again.
    source = root/cases[0]['tasks_json']
    source.write_text(source.read_text().replace('PRIVATE_SPECULATIVE_SUMMARY', 'CHANGED_SUMMARY'))
    refreshed, refreshed_docs = questions.enumerate_task_cases(root, questions.select_task_sources(root, workbook))
    asyncio.run(author.publish(refreshed, refreshed_docs, root/'bank.json', selection={}))
    assert len(client.calls) == 2
    author.checkpoint(cases[0]).write_text('{corrupted')
    asyncio.run(author.author(cases[0]))
    assert len(client.calls) == 3
    altered = questions.TaskQuestionAuthor(client, 'fake', 'fixture', root/'authored', options={'temperature': .1})
    assert altered.directory != author.directory


def test_bank_rejects_missing_or_duplicate_task_coverage_and_changed_workbook(corpus):
    root, workbook, cases, docs = selected(corpus)
    author = questions.TaskQuestionAuthor(AuthorClient(), 'fake', 'fixture', root/'authored')
    selection = dict(workbook=workbook.name, workbook_sha256=questions.file_sha256(workbook))
    bank = asyncio.run(author.publish(cases, docs, root/'bank.json', selection=selection))['bank']
    incomplete = deepcopy(bank); incomplete['cases'].pop(); incomplete['task_count'] -= 1
    with pytest.raises(ValueError, match='every selected'): questions.validate_bank(incomplete)
    duplicate = deepcopy(bank); duplicate['cases'][1] = dict(duplicate['cases'][0], id='different-id')
    with pytest.raises(ValueError, match='Duplicate source task'): questions.validate_bank(duplicate)
    workbook.write_bytes(workbook.read_bytes()+b'changed')
    with pytest.raises(ValueError, match='Workbook changed'): questions.load_task_bank(root/'bank.json', root)


def test_oversized_input_is_explicit_and_never_truncated(corpus):
    root, _, cases, _ = selected(corpus)
    client = AuthorClient()
    author = questions.TaskQuestionAuthor(client, 'fake', 'fixture', root/'authored', max_input_chars=10)
    with pytest.raises(ValueError, match='exceeds'): asyncio.run(author.author(cases[0]))
    assert not client.calls


def test_atomic_replacement_failure_preserves_previous_complete_bank(corpus, monkeypatch):
    root, _, cases, docs = selected(corpus)
    destination = root/'bank.json'
    atomic_json(destination, {'previous_complete': True})
    import utils.object_rag_evaluation as persistence
    replace = persistence.os.replace
    def failing_replace(source, target):
        if Path(target) == destination:
            raise OSError('fixture atomic replace failure')
        return replace(source, target)
    monkeypatch.setattr(persistence.os, 'replace', failing_replace)
    author = questions.TaskQuestionAuthor(AuthorClient(), 'fake', 'fixture', root/'authored')
    with pytest.raises(OSError): asyncio.run(author.publish(cases, docs, destination, selection={}))
    assert read_json(destination) == {'previous_complete': True}
    assert sorted(p.name for p in root.iterdir()) == ['assets', 'authored', 'bank.json', 'manuals.xlsx']


def test_zero_and_positive_duplicate_profile_rows_conflict(corpus):
    root, document, workbook = corpus
    document('4.0')
    path = workbook({'first': [row('4.0', count=0)], 'second': [row('4.0')]})
    with pytest.raises(ValueError, match='Conflicting'): questions.select_task_sources(root, path)


@pytest.mark.parametrize('marker', ['n.a.', ' N.A. ', 'n/a', None, ''])
def test_unavailable_aggregate_counts_allow_profile_selection(corpus, marker):
    root, document, workbook = corpus
    document('4.0'); document('5.0')
    path = workbook({'all': [row(count=marker), row(name='unavailable', count=marker)],
                     'V4': [row('4.0')], 'V5': [row('5.0')]})
    before = path.read_bytes()
    selections = questions.select_task_sources(root, path)
    cases, docs = questions.enumerate_task_cases(root, selections)
    assert [(s['key'], s['profile']) for s in selections] == [('manual', '4.0'), ('manual', '5.0')]
    assert len(cases) == 4 and all(doc['count_matches'] for doc in docs)
    assert path.read_bytes() == before


def test_unknown_profile_count_still_takes_precedence_over_aggregate(corpus):
    root, document, workbook = corpus
    document('4.0'); document('5.0')
    path = workbook({'all': [row()], 'V4': [row('4.0', count='n.a.')], 'V5': [row('5.0')]})
    selections = questions.select_task_sources(root, path)
    assert [(s['key'], s['profile']) for s in selections] == [('manual', '5.0')]


@pytest.mark.parametrize('count', ['two', -1, 1.5, True, float('inf')])
def test_invalid_counts_still_report_the_sheet_row_and_value(corpus, count):
    root, document, workbook = corpus
    document('4.0')
    path = workbook({'V4': [row('4.0', count=count)]})
    with pytest.raises(ValueError, match='V4:2: invalid task_count'):
        questions.select_task_sources(root, path)
