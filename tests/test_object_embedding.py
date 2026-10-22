"""Offline acceptance tests for canonical object embeddings and generation publication."""
import asyncio
import copy
import hashlib
import json
import sqlite3

import chromadb
import pytest

from ribosome.core.dom.embedding.object import ObjectEmbeddingIndex, collect_object_records


class Provider:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.vector = None

    async def embed(self, model, input):
        self.calls.append((model, input))
        if self.fail is not None:
            raise self.fail
        vector = self.vector
        if vector is None:
            digest = hashlib.sha256(input.encode()).digest()
            vector = [1 + digest[i] / 255 for i in range(4)]
        return {'embeddings': [vector], 'model': model + ':resolved'}


class CollectionProxy:
    def __init__(self, collection):
        self.collection = collection
        self.fail = False

    def __getattr__(self, name):
        return getattr(self.collection, name)

    def upsert(self, **kwargs):
        if self.fail:
            raise RuntimeError('vector store failure')
        return self.collection.upsert(**kwargs)


def record(identifier, kind, summary, content=None, **kwargs):
    return {'id': identifier, 'type': kind, 'summary': summary,
            'content': content if content is not None else str(identifier), **kwargs}


def fixture_tree():
    image = record('1.group.1.row.1.cell.1.figure.1', 'figure', '', '<img src="a.png">',
                   summary_ignored=True, images=[{'src': 'a.png', 'resolved_src': '/manual/a.png'}])
    item = record('1.group.1.row.1.cell.1.list.1.item.1', 'list_item', 'needle', '1. raw ~ / item\n', ordinal=1)
    nested = record('1.group.1.row.1.cell.1.list.1', 'list', 'List', children=[item])
    cell = record('1.group.1.row.1.cell.1', 'table_cell', 'Cell', children=[image, nested],
                  source_file='/manual/source.md', line_start=12, line_end=14, column=1, colspan=2)
    extra_cell = record('1.group.1.row.1.cell.2', 'table_cell', 'Plain cell', column=2)
    row = record('1.group.1.row.1', 'table_row', 'Row', children=[cell], cells=[copy.deepcopy(cell), extra_cell])
    group = record('1.group.1', 'table_row_group', 'Group', children=[row])
    table = record(1, 'table', 'Table', '<table>exact source\n</table>\n', children=[group], headers=[[copy.deepcopy(cell)]], caption='表 1')
    mirror = {'t': 'Div', 'object': copy.deepcopy(group), 's': 'Group mirror'}
    mirror['object']['summary'] = 'stale mirrored summary'
    return {'title': 'Manual', 'file_path': '/manual/source.md', 'summary': 'Document', 'blocks': [
        {'t': 'Section', 's': 'Not indexed section', 'c': [
            {'t': 'Header', 'c': [1, ['', [], []], [{'t': 'Str', 'c': 'Setup'}]]},
            {'t': 'Content', 'c': [{'t': 'Div', 'object': table, 's': 'Not indexed wrapper', 'subnodes': [mirror]}]}]},
        {'t': 'Para', 's': 'Not indexed prose', 'c': [{'t': 'Str', 'c': 'Text', 's': 'Not indexed inline'}]}]}


def write(tmp_path, tree=None, name='manual_semantics.json'):
    path = tmp_path / name
    path.write_text(json.dumps(tree if tree is not None else fixture_tree(), ensure_ascii=False, indent=2), encoding='utf-8')
    return path


@pytest.fixture
def index(tmp_path):
    client = chromadb.EphemeralClient()
    collection = client.create_collection('objects-' + tmp_path.name.replace('_', '-')[-35:],
        metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    provider = Provider()
    proxy = CollectionProxy(collection)
    instance = ObjectEmbeddingIndex(tmp_path / 'catalog.sqlite3', collection=proxy, llm_client=provider)
    yield instance, provider, proxy
    instance.close()
    client.delete_collection(collection.name)


def run(coroutine):
    return asyncio.run(coroutine)


def lookup(records, source_id):
    return next(value for value in records if value.source_id == source_id)


def test_canonical_records_and_backtracking_preserve_source(tmp_path):
    tree = fixture_tree()
    before = copy.deepcopy(tree)
    records = collect_object_records(tree, tmp_path / 'manual_semantics.json')
    assert tree == before
    assert len(records) == 9
    assert len({r.id for r in records}) == len(records)
    table = lookup(records, 1)
    assert table.content == '<table>exact source\n</table>\n'
    assert table.section_headings == ['Setup']
    assert table.parent_id == records[0].id
    group = lookup(records, '1.group.1')
    assert group.summary == 'Group'
    assert len(group.occurrence_paths) == 2
    assert group.object_path.endswith('/object/children/0')
    row = lookup(records, '1.group.1.row.1')
    cell = lookup(records, '1.group.1.row.1.cell.1')
    plain = lookup(records, '1.group.1.row.1.cell.2')
    assert cell.object_path.endswith('/children/0')
    assert len(cell.occurrence_paths) > 2
    assert row.children_ids == [cell.id, plain.id]
    assert cell.parent_id == row.id
    assert group.children_ids == [row.id]
    assert cell.attributes['column'] == 1 and cell.attributes['colspan'] == 2
    assert cell.attributes['line_start'] == 12
    assert records[0].content_format == 'json'
    assert json.loads(records[0].content) == tree
    image = next(r for r in records if r.type == 'figure')
    assert image.summary == '' and image.attributes['summary_ignored'] is True
    assert image.attributes['images'][0]['src'] == 'a.png'


def test_paths_ids_and_duplicate_summaries(tmp_path):
    a = record(1, 'figure', 'same')
    b = record('1', 'figure', 'same')
    tree = {'summary': 'Doc', 'a/~b': [a, b]}
    first = collect_object_records(tree, tmp_path / 'a_semantics.json')
    second = collect_object_records(copy.deepcopy(tree), tmp_path / 'a_semantics.json')
    assert first == second
    assert first[1].object_path == '$/a~1~0b/0'
    assert first[1].id != first[2].id
    moved = collect_object_records(tree, tmp_path / 'b_semantics.json')
    assert {r.id for r in first}.isdisjoint({r.id for r in moved})
    tree['a/~b'][0]['summary'] = 'edited'
    assert collect_object_records(tree, tmp_path / 'a_semantics.json')[1].id == first[1].id


def test_row_child_ids_follow_column_order_when_only_later_cells_have_children(tmp_path):
    cells = [record(f'1.row.1.cell.{i}', 'table_cell', f'Cell {i}', column=i) for i in range(1, 4)]
    row = record('1.row.1', 'table_row', 'Row', children=[cells[2]], cells=copy.deepcopy(cells))
    tree = {'summary': 'Doc', 'objects': [record(1, 'table', 'Table', children=[row])]}
    records = collect_object_records(tree, tmp_path / 'order_semantics.json')
    by_id = {r.id: r for r in records}
    canonical_row = lookup(records, '1.row.1')
    assert [by_id[identifier].attributes['column'] for identifier in canonical_row.children_ids] == [1, 2, 3]


@pytest.mark.parametrize('field,value', [('type', 'list'), ('content', 'conflicting raw')])
def test_conflicting_mirrors_fail_with_paths(tmp_path, field, value):
    tree = fixture_tree()
    wrapper = tree['blocks'][0]['c'][1]['c'][0]
    wrapper['subnodes'][0]['object'][field] = value
    with pytest.raises(ValueError, match='conflicting object'):
        collect_object_records(tree, tmp_path / 'bad_semantics.json')


@pytest.mark.parametrize('summary', [None, 42, {}, []])
def test_malformed_summary_is_not_silently_dropped(tmp_path, summary):
    with pytest.raises(ValueError, match='summary must be a string'):
        collect_object_records({'summary': 'Doc', 'objects': [record(1, 'figure', summary)]}, tmp_path / 'bad_semantics.json')


def test_index_search_catalog_and_unchanged_reopen(index, tmp_path):
    store, provider, collection = index
    source = write(tmp_path)
    before = source.read_bytes()
    result = run(store.index_file(source))
    assert result.objects == 8
    assert result.embedded == len(provider.calls) == 8
    assert result.skipped == 1
    assert source.read_bytes() == before
    hits = run(store.search('needle', top_k=1, object_types=['list_item']))
    assert len(hits) == 1 and hits[0].distance == pytest.approx(0, abs=1e-6)
    hit = hits[0]
    assert hit.summary == 'needle'
    assert hit.record.content == '1. raw ~ / item\n'
    assert [r.type for r in hit.ancestors] == ['document', 'table', 'table_row_group', 'table_row', 'table_cell', 'list']
    assert hit.record.section_headings == ['Setup']
    table_hit = run(store.search('Table', top_k=1, object_types=['table']))[0]
    assert [child.type for child in table_hit.children] == ['table_row_group']
    image = next(r for r in collect_object_records(json.loads(source.read_text()), source) if r.type == 'figure')
    assert store.get_object(image.id).summary == ''
    assert store.get_object('missing') is None
    assert run(store.search('needle', document_ids=['unknown'])) == []
    count = len(provider.calls)
    repeat = run(store.index_file(source))
    assert repeat.status == 'unchanged' and repeat.generation_id == result.generation_id
    assert len(provider.calls) == count
    reopened = ObjectEmbeddingIndex(store.catalog_path, collection=collection, llm_client=provider)
    try:
        assert run(reopened.index_file(source)).status == 'unchanged'
        assert reopened.get_object(hit.record.id) == hit.record
    finally:
        reopened.close()


def test_metadata_edits_reuse_vectors_and_removed_objects_disappear(index, tmp_path):
    store, provider, collection = index
    tree = {'summary': 'Doc', 'objects': [record(1, 'figure', 'same'), record(2, 'figure', 'same')]}
    source = write(tmp_path, tree)
    initial = run(store.index_file(source))
    assert initial.embedded == 2 and initial.reused == 1
    old_ids = [r.id for r in collect_object_records(tree, source)]
    tree['objects'][0]['content'] = 'new raw content'
    source = write(tmp_path, tree)
    changed = run(store.index_file(source))
    assert changed.embedded == 0 and changed.reused == 3
    assert changed.generation_id != initial.generation_id
    assert store.get_object(old_ids[1]).content == 'new raw content'
    tree['objects'].pop()
    source = write(tmp_path, tree)
    removed = run(store.index_file(source))
    assert removed.embedded == 0
    assert store.get_object(old_ids[2]) is None
    hits = run(store.search('same', object_types=['figure'], top_k=20))
    assert [h.record.id for h in hits] == [old_ids[1]]
    assert collection.count() > removed.embedded + removed.reused


def test_summary_edits_only_request_changed_text(index, tmp_path):
    store, provider, _ = index
    tree = {'summary': 'Doc', 'objects': [record(1, 'figure', 'before')]}
    source = write(tmp_path, tree)
    first = run(store.index_file(source))
    tree['objects'][0]['summary'] = 'after'
    source = write(tmp_path, tree)
    second = run(store.index_file(source))
    assert second.embedded == 1 and second.reused == 1
    assert provider.calls[-1][1] == 'after'
    assert second.document_id == first.document_id
    assert run(store.search('after', object_types=['figure']))[0].summary == 'after'


@pytest.mark.parametrize('failure', ['provider', 'cancel', 'chroma', 'sqlite'])
def test_failed_replacement_preserves_active_generation_and_retry(index, tmp_path, failure):
    store, provider, collection = index
    tree = {'summary': 'Doc', 'objects': [record(1, 'figure', 'before')]}
    source = write(tmp_path, tree)
    original = run(store.index_file(source))
    tree['objects'][0]['summary'] = 'after'
    source = write(tmp_path, tree)
    if failure == 'provider':
        provider.fail = RuntimeError('provider unavailable')
    elif failure == 'cancel':
        provider.fail = asyncio.CancelledError()
    elif failure == 'chroma':
        collection.fail = True
    else:
        store.connection.execute("CREATE TEMP TRIGGER deny_publish BEFORE INSERT ON active_documents BEGIN SELECT RAISE(ABORT, 'publish denied'); END")
    with pytest.raises((RuntimeError, asyncio.CancelledError, sqlite3.IntegrityError)):
        run(store.index_file(source))
    assert store._active_generation(original.document_id) == original.generation_id
    provider.fail = None
    collection.fail = False
    if failure == 'sqlite':
        store.connection.execute('DROP TRIGGER deny_publish')
    assert run(store.search('before', object_types=['figure']))[0].summary == 'before'
    retried = run(store.index_file(source))
    assert retried.generation_id != original.generation_id
    assert run(store.search('after', object_types=['figure']))[0].summary == 'after'
    if failure == 'sqlite':
        assert retried.embedded == 0


@pytest.mark.parametrize('vector', [[], [float('nan')], [float('inf')], [0.0, 0.0], [1.0, 2.0]])
def test_invalid_vectors_preserve_existing_document(index, tmp_path, vector):
    store, provider, _ = index
    source = write(tmp_path, {'summary': 'Doc', 'objects': []})
    original = run(store.index_file(source))
    source = write(tmp_path, {'summary': 'Changed', 'objects': []})
    provider.vector = vector
    with pytest.raises(ValueError, match='Embedding|embedding'):
        run(store.index_file(source))
    assert store._active_generation(original.document_id) == original.generation_id


def test_empty_summaries_are_catalogued_without_provider_calls(index, tmp_path):
    store, provider, _ = index
    source = write(tmp_path, {'summary': '', 'objects': [record(1, 'figure', '   ', summary_ignored=True)]})
    result = run(store.index_file(source))
    assert result.embedded == 0 and result.skipped == 2 and provider.calls == []
    assert run(store.search('question')) == []
    records = collect_object_records(json.loads(source.read_text()), source)
    assert store.get_object(records[1].id).summary == '   '


def test_corpus_reports_failures_deduplicates_inputs_and_keeps_other_documents(index, tmp_path):
    store, _, _ = index
    a = write(tmp_path, {'summary': 'A', 'objects': [record(1, 'figure', 'A figure')]}, 'a_semantics.json')
    b = write(tmp_path, {'summary': 'B', 'objects': [record(1, 'figure', 'B figure')]}, 'b_semantics.json')
    bad = tmp_path / 'bad_semantics.json'
    bad.write_text('{broken')
    reports = run(store.index_paths([tmp_path, a]))
    assert [r.status for r in reports] == ['indexed', 'indexed', 'failed']
    assert reports[-1].error.startswith('JSONDecodeError:')
    a_doc, b_doc = reports[:2]
    assert a_doc.document_id != b_doc.document_id
    run(store.index_paths([a]))
    assert store.get_object(b_doc.document_id) is not None
    hits = run(store.search('B figure', document_ids=[b_doc.document_id], object_types=['figure']))
    assert len(hits) == 1 and hits[0].record.document_id == b_doc.document_id
    with pytest.raises(ValueError, match='existing'):
        run(store.index_paths(tmp_path / 'missing'))


def test_configuration_and_collection_mismatches_fail(index, tmp_path):
    store, provider, proxy = index
    with pytest.raises(ValueError, match='configuration'):
        ObjectEmbeddingIndex(store.catalog_path, collection=proxy, llm_client=provider, model='other')
    with pytest.raises(ValueError, match='configuration'):
        ObjectEmbeddingIndex(store.catalog_path, collection=proxy, llm_client=provider, embedding_config={'provider': 'different'})
    with pytest.raises(ValueError, match='catalog mismatch'):
        ObjectEmbeddingIndex(tmp_path / 'different.sqlite3', collection=proxy, llm_client=provider)
    client = chromadb.EphemeralClient()
    wrong = client.create_collection('wrong-' + tmp_path.name[-25:], embedding_function=None)
    try:
        with pytest.raises(ValueError, match='cosine'):
            ObjectEmbeddingIndex(tmp_path / 'wrong.sqlite3', collection=wrong, llm_client=provider)
    finally:
        client.delete_collection(wrong.name)


@pytest.mark.parametrize('kwargs', [{'top_k': 0}, {'top_k': True}, {'object_types': 'table'}, {'document_ids': 'abc'}])
def test_search_rejects_invalid_arguments(index, kwargs):
    with pytest.raises(ValueError):
        run(index[0].search('question', **kwargs))


def test_original_json_text_preserves_crlf_and_unicode(index, tmp_path):
    store, _, _ = index
    path = tmp_path / 'unicode_semantics.json'
    raw = '{\r\n  "summary": "文档",\r\n  "objects": []\r\n}\r\n'
    path.write_bytes(raw.encode('utf-8'))
    result = run(store.index_file(path))
    assert store.get_object(result.document_id).content == raw
    assert path.read_bytes() == raw.encode('utf-8')


def test_query_uses_one_generation_when_document_is_replaced_during_embedding(index, tmp_path):
    store, provider, _ = index
    tree = {'summary': 'Doc', 'objects': [record(1, 'figure', 'before')]}
    path = write(tmp_path, tree)
    old = run(store.index_file(path))
    original_embed = provider.embed

    async def replace_during_query(model, input):
        if input == 'query while publishing':
            tree['objects'][0]['summary'] = 'after'
            write(tmp_path, tree)
            await store.index_file(path)
        return await original_embed(model, input)

    provider.embed = replace_during_query
    hits = run(store.search('query while publishing', object_types=['figure']))
    assert hits[0].summary == 'before'
    assert 'before' in hits[0].ancestors[0].content
    assert store._active_generation(old.document_id) != old.generation_id
    assert run(store.search('after', object_types=['figure']))[0].summary == 'after'


def test_retry_reuses_vectors_computed_before_a_later_provider_failure(index, tmp_path):
    store, provider, _ = index
    source = write(tmp_path, {'summary': 'Doc', 'objects': [record(1, 'figure', 'first'), record(2, 'figure', 'second')]})
    original_embed = provider.embed

    async def fail_second(model, input):
        if input == 'second':
            raise RuntimeError('second object failed')
        return await original_embed(model, input)

    provider.embed = fail_second
    with pytest.raises(RuntimeError, match='second object failed'):
        run(store.index_file(source))
    assert run(store.search('first')) == []
    provider.embed = original_embed
    result = run(store.index_file(source))
    assert result.reused == 2 and result.embedded == 1
    assert [text for _, text in provider.calls] == ['Doc', 'first', 'second']


def test_corpus_cancellation_propagates(index, tmp_path):
    store, provider, _ = index
    source = write(tmp_path, {'summary': 'Doc'})
    provider.fail = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run(store.index_paths([source]))
