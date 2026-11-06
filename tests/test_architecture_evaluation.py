from copy import deepcopy
import json

import pytest

from utils.architecture_evaluation import activation_gate, main
from utils.architecture_maintenance import main as maintenance


def report(improved=False):
    return {'identity': {'case_set': 'frozen', 'source_revisions': ['revision-a'], 'answer_model': 'fake',
            'output_token_limit': 100, 'generation_set': ['candidate' if improved else 'old'],
            'retrieval_config': {'mode': 'hybrid' if improved else 'summary'},
            'evidence_schema': 'registry-v1', 'encoder_contract': 'fake-v1'},
            'index_size_bytes': 150 if improved else 100,
            'cases': [{'case_id': 'one', 'split': 'held_out', 'manual_id': 'manual', 'revision': 'a',
                       'object_type': 'table_row', 'gold_evidence_ids': ['r1', 'r2'],
                       'adjudication_provenance': 'independent-fixture-reference',
                       'retrieved_evidence_ids': ['r1', 'r2'] if improved else ['r1'],
                       'packed_evidence_ids': ['r1', 'r2'] if improved else ['r1'],
                       'claims_supported': 2 if improved else 1, 'claims_total': 2,
                       'citations_valid': 2, 'citations_total': 2, 'revision_errors': 0,
                       'latency_ms': 125 if improved else 100, 'input_tokens': 100,
                       'output_tokens': 25 if improved else 0}]}


def test_gate_requires_quality_and_balanced_budget_without_activation():
    result = activation_gate(report(), report(True))
    assert result['eligible_for_activation'] and not result['activated']
    assert result['candidate']['recall_at_8'] == 1
    assert result['breakdown']['object_type']['table_row']['baseline']['recall_at_8'] == .5
    for field in ('latency_ms', 'input_tokens'):
        candidate = report(True)
        candidate['cases'][0][field] += 1
        assert not activation_gate(report(), candidate)['eligible_for_activation']


def test_gate_rejects_leakage_identity_changes_and_missing_judgments():
    for mutate in [lambda r: r['identity'].update(answer_model='different'),
                   lambda r: r['cases'][0].update(split='tuning'),
                   lambda r: r['cases'][0].update(adjudication_provenance=''),
                   lambda r: r['cases'][0].update(gold_evidence_ids=['other']),
                   lambda r: r['cases'][0].update(latency_ms=float('nan'))]:
        candidate = report(True); mutate(candidate)
        with pytest.raises(ValueError): activation_gate(report(), candidate)


def test_format_valid_answers_are_not_semantic_passes():
    candidate = report(True)
    candidate['cases'][0]['claims_supported'] = 1
    assert not activation_gate(report(), candidate)['eligible_for_activation']
    candidate['cases'][0].update(claims_supported=2, revision_errors=1)
    assert not activation_gate(report(), candidate)['eligible_for_activation']


def test_cli_saves_reviewable_gate_result(tmp_path):
    baseline, candidate, output = [tmp_path / name for name in ('baseline.json', 'candidate.json', 'gate.json')]
    baseline.write_text(json.dumps(report())); candidate.write_text(json.dumps(report(True)))
    assert main(['--baseline', str(baseline), '--candidate', str(candidate), '--output', str(output)]) == 0
    assert json.loads(output.read_text())['activated'] is False


def test_migration_preview_writes_nothing_and_apply_preserves_sources(tmp_path, capsys):
    source, semantics = tmp_path / 'manual.md', tmp_path / 'manual_semantics.json'
    source.write_text('Exact source')
    semantics.write_text(json.dumps({'summary': 'Saved', 'objects': [{'id': 'x', 'type': 'list', 'content': 'Exact', 'summary': 'Saved list'}]}))
    before = (source.read_bytes(), semantics.read_bytes())
    repository = tmp_path / 'repository'
    args = ['--repository', str(repository), 'adopt', str(semantics), '--source', str(source)]
    assert maintenance(args) == 0
    assert not repository.exists()
    assert json.loads(capsys.readouterr().out)['valid']
    assert maintenance([*args, '--apply']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['publication']['state'] == 'committed'
    assert before == (source.read_bytes(), semantics.read_bytes())
