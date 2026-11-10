"""Offline fixtures that issue evidence through the production registry."""
from ribosome.retrieval.contracts import EvidenceRegistry


def registered_result(result, *, text=None, hit=None):
    registry = EvidenceRegistry()
    hits = [hit] if hit is not None else result.get('hits', [])
    if not hits:
        hits = [dict(generation='g', manual=dict(profile='5.0', revision='C-1'),
            record=dict(id='row', source_file='manual.md', content=text or 'Synthetic manual evidence',
                        attributes=dict(line_start=1, line_end=2)))]
    for value in hits:
        record = value['record']
        registry.issue(record=record, generation=value['generation'], manual=value['manual'],
                       text=text if text is not None else record.get('content', ''))
    return dict(result, context=registry.render(), **registry.payload())
