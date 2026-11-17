"""Notebook readers survive ID cleanup without running service/batch cells."""

import nbformat
import pytest

from utils.notebooks.notebook_cells import defined_names, find_cell, load_question_banks


def test_declarations_ignore_quoted_text_and_function_locals():
    misleading = nbformat.v4.new_code_cell(
        'text = "RESULT = unsafe()"\ndef helper():\n    RESULT = unsafe()')
    actual = nbformat.v4.new_code_cell('if True:\n    RESULT, ERRORS = {}, {}')
    notebook = nbformat.v4.new_notebook(cells=[misleading, actual])
    for index, cell in enumerate(notebook.cells):
        cell.id = str(index)
    assert 'RESULT' not in defined_names(misleading)
    assert find_cell(notebook, defines=('RESULT', 'ERRORS')) is actual
    notebook.cells.reverse()
    for index, cell in enumerate(notebook.cells):
        cell.id = f'new-{index}'
    assert find_cell(notebook, defines='RESULT') is actual


def test_missing_and_ambiguous_cells_fail_explicitly():
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell('VALUE = 1')])
    assert find_cell(notebook, defines='MISSING', required=False) is None
    with pytest.raises(ValueError, match='found 0'):
        find_cell(notebook, defines='MISSING')
    notebook.cells.append(nbformat.v4.new_code_cell('VALUE = 2'))
    with pytest.raises(ValueError, match='found 2'):
        find_cell(notebook, defines='VALUE', required=False)


def test_question_bank_loading_excludes_service_and_generation_cells():
    notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(source) for source in [
        'GENERAL_OPERATION_CASES = [{"id": "general"}]',
        'ARC_WELDING_CASES = [{"id": "arc"}]',
        'SAMPLED_CASES = []',
        'raise AssertionError("service setup must not execute")',
        'VISION_SAMPLE_CASES = [{"id": "vision"}]\nSAMPLED_CASES.extend(VISION_SAMPLE_CASES)',
        'raise AssertionError("generation must not execute")',
    ]])
    namespace = load_question_banks(notebook)
    assert namespace['GENERAL_OPERATION_CASES'] == [{'id': 'general'}]
    assert namespace['ARC_WELDING_CASES'] == [{'id': 'arc'}]
    assert namespace['SAMPLED_CASES'] == [{'id': 'vision'}]


@pytest.mark.parametrize('id_format', ['{index}', 'arbitrary-{index}'])
def test_evaluation_namespace_loads_all_cases_without_opening_services(id_format):
    from utils.rag.run_object_rag_evaluation import NOTEBOOK, namespace

    notebook = nbformat.read(NOTEBOOK, as_version=4)
    services = find_cell(notebook, defines=('app', 'collection', 'generation_client'))
    services.source = 'raise AssertionError("offline namespace opened services")'
    for index, cell in enumerate(notebook.cells):
        cell.id = id_format.format(index=index)
    _, values = namespace(notebook)
    cases = values['GENERAL_OPERATION_CASES'] + values['ARC_WELDING_CASES'] + values['SAMPLED_CASES']
    assert len(cases) == len({case['id'] for case in cases}) == 828
    assert len(values['SAMPLED_CASES']) == 760
    assert len(values['SAMPLING_MANIFEST']) == 38
    assert 'app' not in values and 'embedding_client' not in values
