"""Checkpoint migration checks without importing the DOM or model providers."""

import json

import pytest

from utils.summary_asyncio import SummaryCheckpoint


@pytest.mark.parametrize('host', [
    ['http://local:11434', 'http://remote:11434'],
    ['http://remote:11434'],
    'http://replacement:11434',
])
def test_routing_migration_preserves_completed_entries(tmp_path, host):
    file = tmp_path / 'manual.md'
    file.write_text('manual')
    file.with_name('manual_semantics.json').write_text('{}')
    path = tmp_path / 'checkpoint.json'
    settings = {'summarizer': 'object', 'object_schema_version': 4,
                'generation': {'think': False, 'max_tokens': 2048}}
    legacy = SummaryCheckpoint(path, 'vision', 'http://remote:11434', True,
                               settings=settings)
    legacy.record(file, {'status': 'completed'})

    def load(**kwargs):
        return SummaryCheckpoint(path, kwargs.get('model', 'vision'), host,
                                 kwargs.get('resume', True),
                                 settings=kwargs.get('settings', settings),
                                 ignore_host=True)

    migrated = load()
    assert migrated.completed(file)['resumed']
    assert load(model='other').completed(file) is None
    assert load(settings={**settings, 'object_schema_version': 5}).completed(file) is None
    assert load(settings={**settings, 'generation': {'think': True}}).completed(file) is None
    assert load(resume=False).completed(file) is None
    # Other consumers retain strict host matching by default.
    assert SummaryCheckpoint(path, 'vision', host, True,
                             settings=settings).completed(file) is None
    migrated.save()
    assert json.loads(path.read_text())['config']['host'] == host
    assert load().completed(file)['resumed']
    file.write_text('changed manual')
    assert load().completed(file) is None


@pytest.mark.parametrize('schema', [None, 1, 2, 3, 4, 5, 6])
def test_object_feature_migration_preserves_checkpoint_entries(tmp_path, schema):
    file = tmp_path / 'manual.md'
    file.write_text('manual')
    file.with_name('manual_semantics.json').write_text('{"summary": "saved"}')
    path = tmp_path / 'checkpoint.json'
    settings = {'summarizer': 'object', 'lang': 'zh', 'generation': {'max_tokens': 2048}}
    legacy_settings = dict(settings)
    if schema is not None:
        legacy_settings['object_schema_version'] = schema
    legacy = SummaryCheckpoint(path, 'vision', 'old-server', True, settings=legacy_settings)
    legacy.record(file, {'status': 'completed'})
    current_settings = {**settings, 'object_schema_version': 5}

    def load(**kwargs):
        return SummaryCheckpoint(path, kwargs.get('model', 'vision'), 'new-server',
                                 kwargs.get('resume', True),
                                 settings=kwargs.get('settings', current_settings),
                                 ignore_host=True, ignore_settings=('object_schema_version',))

    migrated = load()
    assert migrated.completed(file)['resumed']
    assert migrated.entries == legacy.entries
    assert load(model='other').completed(file) is None
    assert load(resume=False).completed(file) is None
    assert load(settings={**current_settings, 'lang': 'en'}).completed(file) is None
    assert load(settings={**current_settings, 'generation': {'max_tokens': 4096}}).completed(file) is None
    if schema != 5:
        assert SummaryCheckpoint(path, 'vision', 'new-server', True,
                                 settings=current_settings, ignore_host=True).completed(file) is None
    migrated.save()
    saved = json.loads(path.read_text())
    assert saved['config']['settings'] == current_settings
    assert saved['entries'] == legacy.entries
    assert load().completed(file)['resumed']
    file.with_name('manual_semantics.json').write_text('changed output')
    assert load().completed(file) is None
