"""Offline, source-adjudicated activation gate for the architecture migration.

Input is a frozen JSON report with `identity`, `cases`, and optional index size.
This module never queries providers, grades itself, or activates an index.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics


IDENTITY_FIELDS = ('case_set', 'source_revisions', 'answer_model', 'output_token_limit')


def _number(row, key):
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{key} must be a finite nonnegative number')
    return value


def _validate_report(report):
    identity = report.get('identity', {})
    if any(key not in identity for key in IDENTITY_FIELDS):
        raise ValueError('Missing frozen case/source/model identity')
    if not identity.get('generation_set') or not identity.get('retrieval_config'):
        raise ValueError('Generation set and retrieval configuration are required')
    if not identity.get('evidence_schema') or not identity.get('encoder_contract'):
        raise ValueError('Evidence schema and encoder contract are required')
    if not report.get('cases'):
        raise ValueError('A nonempty held-out evaluation is required')
    seen = set()
    for row in report['cases']:
        if row['case_id'] in seen:
            raise ValueError('Duplicate evaluation case')
        seen.add(row['case_id'])
        if row.get('split') != 'held_out':
            raise ValueError('Activation cannot use tuning cases')
        if not row.get('gold_evidence_ids') or not row.get('adjudication_provenance'):
            raise ValueError('Independent gold evidence and adjudication provenance are required')
        for key in ('manual_id', 'revision', 'object_type'):
            if not row.get(key): raise ValueError(f'Missing {key}')
        for key in ('retrieved_evidence_ids', 'packed_evidence_ids'):
            if not isinstance(row.get(key), list): raise ValueError(f'{key} must be a list')
        for key in ('claims_supported', 'claims_total', 'citations_valid', 'citations_total',
                    'revision_errors', 'latency_ms', 'input_tokens', 'output_tokens'):
            _number(row, key)
        if row['claims_supported'] > row['claims_total'] or row['citations_valid'] > row['citations_total']:
            raise ValueError('Supported/valid counts cannot exceed totals')


def _metrics(rows, k=8):
    recalls, packed, rr, ndcg = [], [], [], []
    for row in rows:
        gold = set(row['gold_evidence_ids'])
        ranked = list(dict.fromkeys(row['retrieved_evidence_ids']))[:k]
        recalls.append(len(gold & set(ranked)) / len(gold))
        packed.append(len(gold & set(row['packed_evidence_ids'])) / len(gold))
        relevant = [i + 1 for i, identity in enumerate(ranked) if identity in gold]
        rr.append(1 / relevant[0] if relevant else 0)
        dcg = sum(1 / math.log2(rank + 1) for rank in relevant)
        ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, len(gold)) + 1))
        ndcg.append(dcg / ideal)
    claims = sum(r['claims_total'] for r in rows)
    citations = sum(r['citations_total'] for r in rows)
    latencies = sorted(r['latency_ms'] for r in rows)
    return {
        'cases': len(rows), 'recall_at_8': statistics.mean(recalls),
        'packed_evidence_recall': statistics.mean(packed), 'mrr_at_8': statistics.mean(rr),
        'ndcg_at_8': statistics.mean(ndcg),
        'claim_support_precision': sum(r['claims_supported'] for r in rows) / claims if claims else None,
        'citation_precision': sum(r['citations_valid'] for r in rows) / citations if citations else None,
        'invalid_citations': sum(r['citations_total'] - r['citations_valid'] for r in rows),
        'revision_errors': sum(r['revision_errors'] for r in rows),
        'p95_latency_ms': latencies[max(0, math.ceil(.95 * len(latencies)) - 1)],
        'mean_input_tokens': statistics.mean(r['input_tokens'] for r in rows),
        'mean_output_tokens': statistics.mean(r['output_tokens'] for r in rows),
        'mean_total_tokens': statistics.mean(r['input_tokens'] + r['output_tokens'] for r in rows),
        'cases_with_supported_claims': sum(r['claims_supported'] > 0 for r in rows),
    }


def activation_gate(baseline, candidate):
    _validate_report(baseline)
    _validate_report(candidate)
    for key in IDENTITY_FIELDS:
        if baseline['identity'][key] != candidate['identity'][key]:
            raise ValueError(f'Incomparable evaluation identity: {key}')
    left = {r['case_id']: r for r in baseline['cases']}
    right = {r['case_id']: r for r in candidate['cases']}
    if set(left) != set(right):
        raise ValueError('Baseline and candidate must contain identical cases')
    for identity, row in left.items():
        for key in ('gold_evidence_ids', 'manual_id', 'revision', 'object_type', 'adjudication_provenance'):
            if row[key] != right[identity][key]:
                raise ValueError(f'Changed frozen reference for {identity}: {key}')
    a, b = _metrics(list(left.values())), _metrics(list(right.values()))
    checks = {
        'evidence_recall_improves': b['recall_at_8'] > a['recall_at_8'],
        'claim_support_improves': a['claim_support_precision'] is not None and b['claim_support_precision'] is not None
                                  and b['claim_support_precision'] > a['claim_support_precision'],
        'citation_integrity_no_regression': b['invalid_citations'] <= a['invalid_citations']
                    and (a['citation_precision'] is None or (b['citation_precision'] is not None
                         and b['citation_precision'] >= a['citation_precision'])),
        'revision_integrity_no_regression': b['revision_errors'] <= a['revision_errors'],
        'p95_latency_within_budget': b['p95_latency_ms'] <= 1.25 * a['p95_latency_ms'],
        'tokens_within_budget': b['mean_total_tokens'] <= 1.25 * a['mean_total_tokens'],
    }
    breakdown = {}
    for key in ('object_type', 'revision'):
        groups = defaultdict(lambda: ([], []))
        for identity, row in left.items():
            groups[row[key]][0].append(row)
            groups[row[key]][1].append(right[identity])
        breakdown[key] = {label: {'baseline': _metrics(pair[0]), 'candidate': _metrics(pair[1])}
                          for label, pair in sorted(groups.items())}
    return {'eligible_for_activation': all(checks.values()), 'activated': False, 'checks': checks,
            'baseline': a, 'candidate': b, 'breakdown': breakdown,
            'index_size_bytes': {'baseline': baseline.get('index_size_bytes'), 'candidate': candidate.get('index_size_bytes')},
            'identity': {'baseline': baseline['identity'], 'candidate': candidate['identity']}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    result = activation_gate(json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text()))
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.output: args.output.write_text(text)
    else: print(text, end='')
    return 0 if result['eligible_for_activation'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
