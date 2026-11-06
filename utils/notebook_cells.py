"""Resolve evaluation cells from their semantic role, independent of nbdev cell IDs."""
import re


def label_cells(notebook, *, application=False):
    """Assign stable role names in memory; no notebook file is written."""
    for cell in notebook.cells:
        source = cell.source
        role = None
        if cell.cell_type == 'code':
            markers = [
                ('def rerank_by_category(', 'evaluation-config'),
                ('from object_rag_evaluation import EvaluationRun', 'evaluation-imports'),
                ('def coverage_case_metrics(', 'coverage-test-helpers' if application else 'evaluation-helpers'),
                ('app = ObjectRAG(', 'evaluation-services'),
                ('COVERAGE_CASES = ', 'coverage-prompt-bank'),
                ('COVERAGE_CASE_IDS = ', '513b4572'),
                ('INSPECT_CASE_ID = coverage_cases[0]', '8ec7778c'),
                ('coverage_retrieval_results, coverage_retrieval_errors = {}, {}', '81ab0242'),
                ('coverage_answer_results, coverage_answer_errors = {}, {}', 'f1538ef2'),
                ('GENERAL_OPERATION_CASES = ', 'general-operation-prompt-bank'),
                ('ARC_WELDING_CASES = ', 'arc-welding-prompt-bank'),
                ('GENERAL_OPERATION_TOP_K = ', 'general-operation-selection'),
                ('ARC_WELDING_TOP_K = ', 'arc-welding-selection'),
                ('general_operation_retrieval_metrics = ', 'general-operation-retrieval'),
                ('general_operation_answer_metrics = ', 'general-operation-answers'),
                ('arc_welding_retrieval_metrics = ', 'arc-welding-retrieval'),
                ('arc_welding_answer_metrics = ', 'arc-welding-answers'),
                ('SAMPLED_CASES = []', 'sampled-banks-start'),
                ('ALL_EVALUATION_CASES = ', 'full-evaluation-run'),
                ('evaluation.show_answers(GENERAL_OPERATION_CASES + ARC_WELDING_CASES)', 'recorded-legacy-results'),
                ("for client_name in (", 'e7686d54' if application else 'evaluation-cleanup'),
            ]
            for marker, label in markers:
                matches = re.search(r'^'+re.escape(marker), source, re.M) if marker.startswith('def ') else marker in source
                if matches:
                    role = label; break
            match = re.search(r'^([A-Z_]+)_SAMPLE_CASES\s*=', source, re.M)
            if match: role = f'sampled-{match[1].lower()}-bank'
            if 'await query_embeddings.prepare' in source and 'ALL_EVALUATION_CASES' not in source:
                match = re.search(r'\b([a-z_]+)_sample_cases\b', source)
                if match: role = f'sampled-{match[1]}-run'
        else:
            markers = [('## General-operation manual version tests', 'general-operation-tests'),
                       ('## Arc-welding manual version tests', 'arc-welding-tests'),
                       ('## CSV manual sampling tests', 'sampled-manual-tests'),
                       ('## Algorithm and document review', 'evaluation-review')]
            for marker, label in markers:
                if source.startswith(marker): role = label; break
        if role is not None:
            cell.id = role
    return notebook
