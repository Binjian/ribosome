"""Offline checks for the RAG application's source-backed coverage prompts."""
import ast
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import re
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

import nbformat
import pytest

NOTEBOOK = Path('nbs/05.application.rag.object.ipynb')


def coverage_namespace():
    notebook = nbformat.read(NOTEBOOK, as_version=4)
    evaluation = nbformat.read('nbs/06.test.rag.object.question_with_location.ipynb', as_version=4)
    nbformat.validate(evaluation)
    # Exercise legacy helpers against the moved banks without opening services.
    notebook.cells = notebook.cells[:-2] + [c for c in evaluation.cells if c.id.startswith(
        ('general-operation-', 'arc-welding-'))] + notebook.cells[-2:]
    ns = {'USE_PERSISTENT_EVALUATION': False}
    exec(notebook.cells[2].source, ns)
    for cell_id in ['coverage-prompt-bank', 'coverage-test-helpers',
                    'general-operation-prompt-bank', 'arc-welding-prompt-bank']:
        exec(next(c.source for c in notebook.cells if c.id == cell_id), ns)
    return notebook, ns


def test_every_category_and_both_manual_subsection_layouts_are_covered():
    _, ns = coverage_namespace()
    # The supported taxonomy is an independent expectation, not a dependency on
    # private category CSVs and workbooks absent from standalone checkouts.
    categories = {
        'adhesive_dispensing', 'arc_welding', 'fds', 'general_operation',
        'grinding', 'laser_welding', 'load_analysis', 'material_handling',
        'offline_interface', 'palletizing', 'pneumatic_spot_welding',
        'spot_welding', 'spr_riveting', 'stud_welding', 'teaching_free_welding',
        'tool_quick_change', 'tracking', 'vision',
    }
    cases = ns['COVERAGE_CASES'] + ns['GENERAL_OPERATION_CASES']
    assert len(ns['COVERAGE_CASES']) == 17
    assert all(case['category'] != 'general_operation' for case in ns['COVERAGE_CASES'])
    assert len(ns['GENERAL_OPERATION_CASES']) == 38
    assert {case['category'] for case in ns['GENERAL_OPERATION_CASES']} == {'general_operation'}
    assert len(cases) == len({case['id'] for case in cases}) == 55
    assert {case['category'] for case in cases} == set(categories)
    assert Counter(case['category'] for case in cases if case['kind'] == 'category') == {
        category: 1 for category in categories if category != 'general_operation'
    }
    expected = {(profile, f'9.1.{number}'): 2
                for profile, count in [('4.0', 8), ('5.0', 6)]
                for number in range(1, count+1)}
    assert Counter((case['profile'], case['section']) for case in cases
                   if case['kind'] == 'instruction') == expected
    assert Counter(case['profile'] for case in cases if case['kind'] == 'alarm') == {
        '4.0': 5, '5.0': 5
    }


def test_docx_references_and_seeded_alarm_samples_match_the_supplied_manuals():
    _, ns = coverage_namespace()
    w = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    for profile, sample in ns['ALARM_SAMPLING'].items():
        path = ns['PROJ_ROOT']/sample['source_docx']
        if not path.exists():
            pytest.skip('Source manuals are not installed')
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sample['sha256']
        with zipfile.ZipFile(path) as docx:
            body = ET.fromstring(docx.read('word/document.xml')).find(w+'body')

        def row_cells(row):
            return ['\n'.join(''.join(t.text or '' for t in p.iter(w+'t'))
                              for p in cell.findall(w+'p'))
                    for cell in row.findall(w+'tc')]

        rows = body[sample['body_block']].findall(w+'tr')
        population = [(i, row_cells(row)) for i, row in enumerate(rows)
                      if len(row_cells(row)) == 4 and re.fullmatch(r'\d+', row_cells(row)[0].strip())]
        expected = sorted(random.Random(sample['seed']).sample(population, 5))
        assert len(population) == sample['population']
        assert [i for i, _ in expected] == sample['rows']
        alarms = [case for case in ns['GENERAL_OPERATION_CASES']
                  if case['profile'] == profile and case['kind'] == 'alarm']
        assert [case['alarm_code'] for case in alarms] == [cells[0] for _, cells in expected]
        for case in [c for c in ns['GENERAL_OPERATION_CASES'] if c['profile'] == profile]:
            for reference in case['references']:
                table = body[reference['body_block']].findall(w+'tr')
                actual = '\n'.join(' | '.join(row_cells(table[i])) for i in reference['rows'])
                assert actual == reference['excerpt'], case['id']
        # A blank reference is deliberately an unknown, not a generated answer key.
        for case in alarms:
            cells = row_cells(rows[case['references'][0]['rows'][0]])
            assert case['reference_alarm'] == dict(zip(['message', 'cause', 'remedy'], cells[1:]))


def synthetic_evidence(case, *, content=None, profile=None):
    source = Path(case['references'][0]['docx']).with_suffix('.md') if case['references'] else Path('manual.md')
    hit = dict(generation='test-generation', distance=0.1,
               record=dict(id='row', type='table_row', source_file=str(source),
                           summary='Stored summary for this question.',
                           content=content if content is not None else ' '.join(case['anchors']),
                           attributes=dict(line_start=10, line_end=12),
                           section_headings=[case['section']+' '+title for title in case['heading_path']]
                           if case.get('heading_path') else
                           ([case['section']+' Test section'] if case['section'] else [])),
               manual=dict(profile=profile or case['profile'] or '4.0', revision=case['revision'] or 'B-5'))
    return dict(question=case['question'], category=case['category'], hits=[hit],
                context='[E1] Only retrieved evidence belongs in the answer prompt.',
                coverage=dict(indexed=[hit['manual']], missing=[]), omissions=[])


def test_coverage_metrics_require_matching_revision_section_and_complete_alarm_code():
    _, ns = coverage_namespace()
    case = next(c for c in ns['GENERAL_OPERATION_CASES'] if c.get('alarm_code') == '6035')
    score = ns['coverage_case_metrics']
    wrong_revision = score(case, synthetic_evidence(case, content='6035', profile='4.0'))
    assert not wrong_revision['expected_manual_hit']
    assert not wrong_revision['anchors_in_matching_hits']
    wrong_code = score(case, synthetic_evidence(case, content='16035'))
    assert wrong_code['expected_manual_hit']
    assert not wrong_code['anchors_in_matching_hits']
    good = score(case, synthetic_evidence(case, content='6035'))
    assert good['expected_manual_hit'] and good['expected_section_hit'] and good['anchors_in_matching_hits']
    missing_section = synthetic_evidence(case, content='6035')
    missing_section['hits'][0]['record']['section_headings'] = ['10.2 警告']
    assert not score(case, missing_section)['expected_section_hit']


def test_batch_continues_after_failures_replays_evidence_and_does_not_leak_reference_answers():
    notebook, ns = coverage_namespace()
    wanted = ['category_arc_welding', 'category_fds',
              'general_40_9_1_1_single_output', 'general_50_alarm_6035']
    bank = ns['COVERAGE_CASES'] + ns['GENERAL_OPERATION_CASES']
    cases = [deepcopy(next(c for c in bank if c['id'] == case_id)) for case_id in wanted]
    cases[-1]['references'][0]['excerpt'] = 'PRIVATE_REFERENCE_CANARY'
    retrieved, generated, displayed = [], [], []

    async def retrieve(question, category, *, top_k=8):
        case = next(c for c in cases if c['question'] == question)
        retrieved.append(case['id'])
        if case['id'] == 'category_fds':
            raise RuntimeError('Synthetic retrieval failure')
        return synthetic_evidence(case)

    class Generator:
        async def chat(self, **kwargs):
            prompt = kwargs['messages'][-1]['content']
            assert 'PRIVATE_REFERENCE_CANARY' not in prompt
            assert '[E1] Only retrieved evidence' in prompt
            generated.append(prompt)
            if cases[2]['question'] in prompt:
                raise RuntimeError('Synthetic generation failure')
            return dict(message=dict(content=json.dumps(dict(
                supported=False, explanation='Source-based diagnostic explanation.',
                scenario='', environment='', expected_result='', code='', instruction_statement='',
                language='', manual='', citations=[]))))

    ns.update(coverage_cases=cases, COVERAGE_TOP_K=8, INSPECT_CASE_ID=cases[0]['id'],
              retrieve=retrieve, display=lambda value: displayed.append(value),
              app=SimpleNamespace(retrieve=retrieve, generator=Generator(), rag_model='test', max_output_tokens=128))

    async def run():
        for cell_id in ['81ab0242', 'f1538ef2']:
            cell = next(c for c in notebook.cells if c.id == cell_id)
            await eval(compile(cell.source, str(NOTEBOOK), 'exec',
                               flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), ns)

    asyncio.run(run())
    assert retrieved == wanted  # Generation does not retrieve or embed a second time.
    assert len(generated) == 3
    assert 'category_fds' in ns['coverage_retrieval_errors']
    assert 'general_40_9_1_1_single_output' in ns['coverage_answer_errors']
    assert ns['coverage_answer_results']['general_50_alarm_6035']['example_status'] == 'unsupported'
    failed = ns['coverage_answer_results']['general_40_9_1_1_single_output']
    assert 'Stored summary' in failed['retrieved_summary']
    assert any('Stored summary' in getattr(value, 'data', '') for value in displayed)


def test_category_and_manual_sections_keep_separate_selections_top_k_and_results():
    notebook, ns = coverage_namespace()
    cases = ns['COVERAGE_CASES'] + ns['GENERAL_OPERATION_CASES'] + ns['ARC_WELDING_CASES']
    retrieval_calls, generation_calls = [], []

    async def retrieve(question, category, *, top_k=8):
        case = next(case for case in cases if case['question'] == question)
        retrieval_calls.append((case['id'], top_k))
        return synthetic_evidence(case)

    class Generator:
        async def chat(self, **kwargs):
            generation_calls.append(kwargs['messages'][-1]['content'])
            return dict(message=dict(content=json.dumps(dict(
                supported=False, explanation='Source-based diagnostic explanation.',
                scenario='', environment='', expected_result='', code='', instruction_statement='',
                language='', manual='', citations=[]))))

    categories = {case['category']: case['category'] for case in cases}
    ns.update(display=lambda value: None,
              app=SimpleNamespace(categories=categories, retrieve=retrieve,
                                  snapshot=lambda category: (None, None, dict(indexed=[], missing=[])),
                                  generator=Generator(), rag_model='test', max_output_tokens=128))

    def execute(cell_id, *replacements):
        source = next(c.source for c in notebook.cells if c.id == cell_id)
        for old, new in replacements:
            source = source.replace(old, new)
        exec(source, ns)

    async def execute_async(cell_id):
        source = next(c.source for c in notebook.cells if c.id == cell_id)
        await eval(compile(source, str(NOTEBOOK), 'exec',
                           flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), ns)

    execute('513b4572', ('COVERAGE_CASE_IDS = None', "COVERAGE_CASE_IDS = ['category_arc_welding']"),
            ('COVERAGE_TOP_K = 8', 'COVERAGE_TOP_K = 3'))
    execute('8ec7778c')

    async def run():
        await execute_async('81ab0242')
        await execute_async('f1538ef2')
        category_results = ns['coverage_answer_results']
        saved_results = deepcopy(category_results)
        execute('general-operation-selection',
                ('GENERAL_OPERATION_CASE_IDS = None',
                 "GENERAL_OPERATION_CASE_IDS = ['general_50_alarm_6035']"),
                ('GENERAL_OPERATION_TOP_K = 8', 'GENERAL_OPERATION_TOP_K = 5'))
        await execute_async('general-operation-retrieval')
        await execute_async('general-operation-answers')
        assert ns['coverage_answer_results'] is category_results
        assert category_results == saved_results
        manual_results = ns['general_operation_answer_results']
        saved_manual_results = deepcopy(manual_results)
        execute('arc-welding-selection',
                ('ARC_WELDING_CASE_IDS = None', "ARC_WELDING_CASE_IDS = ['arc_50_alarm_20081']"),
                ('ARC_WELDING_TOP_K = 8', 'ARC_WELDING_TOP_K = 7'))
        await execute_async('arc-welding-retrieval')
        await execute_async('arc-welding-answers')
        assert ns['coverage_answer_results'] is category_results
        assert category_results == saved_results
        assert ns['general_operation_answer_results'] is manual_results
        assert manual_results == saved_manual_results

    asyncio.run(run())
    assert [case['id'] for case in ns['coverage_cases']] == ['category_arc_welding']
    assert [case['id'] for case in ns['general_operation_cases']] == ['general_50_alarm_6035']
    assert retrieval_calls == [('category_arc_welding', 3), ('general_50_alarm_6035', 5),
                               ('arc_50_alarm_20081', 7)]
    assert len(generation_calls) == 3
    assert ns['general_operation_answer_errors'] == {}
    assert set(ns['coverage_answer_results']) == {'category_arc_welding'}
    assert set(ns['general_operation_answer_results']) == {'general_50_alarm_6035'}
    assert ns['arc_welding_answer_errors'] == {}
    assert set(ns['arc_welding_answer_results']) == {'arc_50_alarm_20081'}
    manual_index = next(i for i, c in enumerate(notebook.cells) if c.id == 'general-operation-tests')
    arc_index = next(i for i, c in enumerate(notebook.cells) if c.id == 'arc-welding-tests')
    cleanup_index = next(i for i, c in enumerate(notebook.cells) if c.id == 'e7686d54')
    assert manual_index < arc_index < cleanup_index
    assert not any(c.cell_type == 'markdown' and c.source.startswith('## ')
                   for c in notebook.cells[arc_index+1:cleanup_index])


def test_arc_cases_match_source_rows_and_reproduce_stratified_samples():
    _, ns = coverage_namespace()
    cases = ns['ARC_WELDING_CASES']
    assert len(cases) == len({c['id'] for c in cases}) == 30
    assert Counter((c['profile'], c['kind']) for c in cases) == {
        ('4.0', 'instruction'): 10, ('5.0', 'instruction'): 10,
        ('4.0', 'alarm'): 2, ('5.0', 'alarm'): 8}
    assert {c['requested_section'] for c in cases if c['kind'] == 'instruction'} == {
        '2', '3', '4', '5', '6', '7', '10'}
    assert {c['section'] for c in cases if c['profile'] == '5.0' and c['kind'] == 'instruction'} == {
        '3', '4', '5', '6', '7'}
    assert Counter((c['profile'], c['classification']) for c in cases if c['kind'] == 'alarm') == {
        ('4.0', '报警信息'): 2, ('5.0', '报警信息'): 8}
    w = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'

    def cells(row):
        return ['\n'.join(''.join(t.text or '' for t in p.iter(w+'t')) for p in tc.findall(w+'p'))
                for tc in row.findall(w+'tc')]

    for profile, sampling in ns['ARC_WELDING_SAMPLING'].items():
        source = ns['PROJ_ROOT']/sampling['source_docx']
        if not source.exists():
            pytest.skip('Arc-welding source manuals are not installed')
        assert hashlib.sha256(source.read_bytes()).hexdigest() == sampling['sha256']
        with zipfile.ZipFile(source) as docx:
            body = ET.fromstring(docx.read('word/document.xml')).find(w+'body')
        tables, chapter, title, headings = {}, 0, '', []
        for index, node in enumerate(body):
            if node.tag == w+'p':
                style = node.find(w+'pPr/'+w+'pStyle')
                sid = style.get(w+'val') if style is not None else ''
                text = ''.join(t.text or '' for t in node.iter(w+'t')).strip()
                if sid == '2':
                    chapter += 1
                    title, headings = text, []
                elif sid in ['3', '4', '5', '6']:
                    headings = headings[:int(sid)-3] + [text]
            elif node.tag == w+'tbl':
                tables[index] = (chapter, [title, *headings], [cells(r) for r in node.findall(w+'tr')])
        version_cases = [c for c in cases if c['profile'] == profile]
        for case in version_cases:
            ref = case['references'][0]
            chapter, path, rows = tables[ref['body_block']]
            assert case['heading_path'] == path
            assert case['section'].split('.')[0] == str(chapter)
            assert ref['excerpt'] == '\n'.join(' | '.join(rows[i]) for i in ref['rows'])
            assert case['category'] == 'arc_welding'
            if case['kind'] == 'alarm':
                row = rows[ref['rows'][0]]
                assert len(row) == len(rows[0])
                assert case['reference_alarm'] == dict(zip(rows[0], row))
                assert case['alarm_code'] == row[0]
                assert case['classification'] in case['question']
        seen = set()
        for stratum in sampling['instruction_strata']:
            pool, unique = [], set(seen)
            for block, (chapter, _, rows) in tables.items():
                if str(chapter) != stratum['section']:
                    continue
                for i, row in enumerate(rows):
                    if len(row) < 3 or row[1].strip() != '功能' or not row[0].strip() or i+1 >= len(rows):
                        continue
                    if len(rows[i+1]) < 3 or rows[i+1][1].strip() != '格式':
                        continue
                    label = ' '.join(row[0].split())
                    if label == 'GRADU APT ON MULTI APT ON GROAPT ON' or '=' in label:
                        continue
                    name = ' '.join(rows[i+1][0].split()) if label == '点激光传感指令' else label
                    if name not in unique:
                        unique.add(name)
                        pool.append(dict(body_block=block, row=i, instruction=name))
            selected = sorted(random.Random(stratum['seed']).sample(pool, stratum['count']),
                              key=lambda entry: (entry['body_block'], entry['row']))
            assert len(pool) == stratum['population']
            assert selected == stratum['selected']
            selected_cases = [c for c in version_cases if c['kind'] == 'instruction'
                              and c['section'] == stratum['section']]
            assert [(c['references'][0]['body_block'], c['references'][0]['rows'][0], c['instruction'])
                    for c in selected_cases] == [(s['body_block'], s['row'], s['instruction']) for s in selected]
            seen.update(entry['instruction'] for entry in selected)
        assert len(seen) == 10
        for stratum in sampling['alarm_strata']:
            _, _, rows = tables[stratum['body_block']]
            pool, seen_codes = [], set()
            for i, row in enumerate(rows[1:], 1):
                code = row[0].strip()
                if re.fullmatch(r'\d+', code) and code not in seen_codes:
                    seen_codes.add(code)
                    pool.append(i)
            assert len(pool) == stratum['population']
            assert sorted(random.Random(stratum['seed']).sample(pool, stratum['count'])) == stratum['rows']
            assert [c['references'][0]['rows'][0] for c in version_cases
                    if c['kind'] == 'alarm' and c['classification'] == stratum['classification']] == stratum['rows']


def test_arc_heading_checks_accept_renumbered_titles_but_reject_wrong_chapters_and_revisions():
    _, ns = coverage_namespace()
    case = next(c for c in ns['ARC_WELDING_CASES'] if c['profile'] == '4.0' and c['section'] == '2')
    evidence = synthetic_evidence(case)
    evidence['hits'][0]['record']['section_headings'] = [
        '1 '+case['heading_path'][0],
        *['1.7.'+str(i)+' '+title for i, title in enumerate(case['heading_path'][1:], 1)]]
    assert ns['coverage_case_metrics'](case, evidence)['expected_section_hit']
    wrong_title = deepcopy(evidence)
    wrong_title['hits'][0]['record']['section_headings'][0] = '2 Wrong chapter'
    assert not ns['coverage_case_metrics'](case, wrong_title)['expected_section_hit']
    wrong_revision = deepcopy(evidence)
    wrong_revision['hits'][0]['manual']['profile'] = '5.0'
    assert not ns['coverage_case_metrics'](case, wrong_revision)['expected_manual_hit']
    assert ns['coverage_heading_title']('1 智能焊接—激光篇') == ns['coverage_heading_title']('智能焊接-激光篇')
    assert ns['coverage_heading_title']('3D寻位') == '3D寻位'
    assert not ns['coverage_heading_path_matches'](['三维寻位', '三维寻位'], ['4.2 三维寻位'])
    assert ns['coverage_heading_path_matches'](['三维寻位', '三维寻位'],
                                              ['4.2 三维寻位', '4.2.4 三维寻位'])
    assert not ns['coverage_heading_path_matches'](['辅助焊接功能', '摆动库'],
                                                  ['1 摆动库', '1.2 辅助焊接功能'])
