"""Offline integration through summary resume, both views, and issued citations."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re

import chromadb

from ribosome.core.artifacts import ArtifactRepository
from ribosome.core.dom.embedding.object import ObjectEmbeddingIndex
from ribosome.core.dom.summary import object as summary
from ribosome.rag.object import ObjectRAG
from ribosome.retrieval.contracts import EncoderSpec, EvidenceRegistry
from utils.summary_asyncio import SummaryCheckpoint
from utils.summary_object_asyncio import summarize_object_documents


def dictionaries(value):
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from dictionaries(child)
    elif isinstance(value, list):
        for child in value: yield from dictionaries(child)


class Progress:
    def __init__(self, total=0, **kwargs): self.n = 0
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def update(self, amount=1): self.n += amount
    def refresh(self): pass
    def close(self): pass
    def set_postfix(self, **kwargs): pass
    def set_postfix_str(self, *args, **kwargs): pass
    @staticmethod
    def write(message): pass


def test_missing_only_resume_to_hybrid_task_retrieval_and_same_revision_citations(tmp_path, monkeypatch):
    folder = tmp_path / 'assets' / 'code-docs.5.0' / '.md'
    folder.mkdir(parents=True)
    fixture = Path(__file__).parent / 'fixtures' / 'architecture'
    source = folder / 'manual.md'
    source.write_bytes((fixture / 'manual.md').read_bytes())
    (folder / 'diagram.svg').write_bytes((fixture / 'diagram.svg').read_bytes())
    monkeypatch.setenv('PROJ_ROOT', str(tmp_path))
    text_calls, image_calls = [], []
    async def text(client, content, **kwargs):
        text_calls.append(content)
        return f'Saved fixture summary {len(text_calls)}: reset valve RST-002 after inspection.'
    async def image(client, image_link, **kwargs):
        image_calls.append(image_link)
        return f'Saved diagram {len(image_calls)}: connector FIXTURE-42.'
    monkeypatch.setattr(summary, 'get_summary_response_async', text)
    monkeypatch.setattr(summary, 'get_image_summary_async', image)
    repository = ArtifactRepository(tmp_path / 'canonical')
    checkpoint = tmp_path / 'summary-checkpoint.json'
    async def summarize():
        return await summarize_object_documents([source], object(), 'fake', checkpoint_path=checkpoint,
            leaf_min_len=0, min_len=0, lang='en', progress_factory=Progress, artifact_repository=repository)
    initial = asyncio.run(summarize())[0]
    assert initial['status'] == 'completed', initial.get('error', initial)
    output = source.with_name('manual_semantics.json')
    tree = json.loads(output.read_text())
    nested_figure = next(n for n in dictionaries(tree) if n.get('type') == 'figure')
    missing_id = nested_figure['id']
    def strip(value):
        if isinstance(value, list):
            return [strip(v) for v in value if not (isinstance(v, dict) and
                    (v.get('id') == missing_id or v.get('object', {}).get('id') == missing_id))]
        if isinstance(value, dict): return {key: strip(child) for key, child in value.items()}
        return value
    legacy = strip(tree)
    saved_summaries = {v for n in dictionaries(legacy) for key, v in n.items()
                       if key in {'s', 'summary'} and isinstance(v, str) and v.strip()}
    output.write_text(json.dumps(legacy))
    saved = json.loads(checkpoint.read_text())
    saved['entries'][str(source.resolve())]['signature'] = SummaryCheckpoint.signature(source)
    checkpoint.write_text(json.dumps(saved))
    before_text, before_image = len(text_calls), len(image_calls)
    row = asyncio.run(summarize())[0]
    assert row['status'] == 'completed' and row['incremental']
    upgraded = json.loads(output.read_text())
    actual_summaries = {v for n in dictionaries(upgraded) for key, v in n.items()
                        if key in {'s', 'summary'} and isinstance(v, str) and v.strip()}
    assert saved_summaries <= actual_summaries
    # The missing figure uses one visual interpretation and one caption/visual reduction.
    assert len(text_calls) == before_text + 1 and len(image_calls) == before_image + 1
    assert text_calls[-1].startswith('Summarize this figure')
    count = (len(text_calls), len(image_calls))
    assert asyncio.run(summarize())[0]['resumed']
    assert count == (len(text_calls), len(image_calls))

    # Persist a legacy task projection to exercise producer/consumer compatibility.
    tasks = deepcopy(upgraded)
    for node in dictionaries(tasks):
        if node.get('type') == 'table': node['type'] = 'task'
    task_path = source.with_name('manual_tasks.json')
    task_path.write_text(json.dumps(tasks))
    frozen = (source.read_bytes(), output.read_bytes(), checkpoint.read_bytes(), task_path.read_bytes())

    class Provider:
        def __init__(self): self.calls = []
        async def embed(self, **kwargs):
            assert kwargs['truncate'] is False
            self.calls.append(kwargs)
            text = kwargs['input'].lower()
            return {'embeddings': [[1.0, .9 if 'rst-002' in text or 'valve' in text else .1, .1]],
                    'model': 'fake', 'model_digest': 'fixture-model-digest'}
    class Generator:
        async def chat(self, **kwargs):
            label = re.search(r'\[(E[1-9][0-9]*)\]', kwargs['messages'][1]['content'])[1]
            return {'message': {'content': json.dumps({'supported': True, 'explanation': 'The fixture documents a reset procedure.',
                'claims': [{'text': 'Inspect the valve before resetting it.', 'citations': [label]}]})}}
    monkeypatch.setattr('ribosome.rag.object.load_mapping', lambda _: (
        {'service': 'Service'}, [{'key': 'manual', 'profile': '5.0', 'category': 'service', 'revision': 'A'}]))
    client = chromadb.EphemeralClient()
    collection = client.create_collection('architecture-vertical-' + tmp_path.name[-15:],
        metadata={'hnsw:space': 'cosine'}, embedding_function=None)
    provider = Provider()
    spec = EncoderSpec('fake', 'fixture-model-digest', 3)
    index = ObjectEmbeddingIndex(tmp_path / 'index' / 'catalog.sqlite3', collection=collection, llm_client=provider,
        model='fake', encoder_spec=spec, representations=('summary', 'source'), artifact_repository=repository)
    rag = None
    try:
        indexed = asyncio.run(index.index_file(task_path))
        assert indexed.status == 'indexed'
        kinds = {r[0] for r in index.connection.execute('SELECT DISTINCT kind FROM object_metadata')}
        assert {'table', 'table_row', 'list_item', 'figure', 'paragraph'} <= kinds
        assert {r[0] for r in index.connection.execute('SELECT DISTINCT kind FROM object_representations')} == {'summary', 'source'}
        document = repository.adopt_document(str(source.resolve()))
        canonical = repository.read(document.id)
        graph_revision, = index.connection.execute('SELECT revision_id FROM document_graphs').fetchone()
        assert graph_revision == canonical.revision.id
        rag = ObjectRAG(tmp_path / 'index', collection, provider, Generator(), mapping_dir=tmp_path,
            embed_model='fake', embedding_config=index.embedding_config, encoder_spec=spec,
            retrieval_mode='hybrid', rag_model='fake')
        exact = asyncio.run(rag.retrieve('RST-002', 'service', top_k=8))
        assert exact['retrieval_trace']['channels']['exact']
        assert 'RST-002' in exact['context'] and exact['evidence']
        assert all(item['text'].strip() for item in exact['evidence'])
        assert 'E999' not in {item['id'] for item in exact['evidence']}
        answer = asyncio.run(rag.answer('How do I restore the valve after inspection?', 'service', top_k=8, mode='procedure'))
        assert answer['example_status'] == 'included'
        assert answer['claim_support'] == 'not_semantically_verified'
        registry = EvidenceRegistry.from_result(answer)
        assert registry.cited(answer['retrieval_trace']['cited'])
        assert all(item.revision['revision'] == 'A' for item in registry.items)
        assert frozen == (source.read_bytes(), output.read_bytes(), checkpoint.read_bytes(), task_path.read_bytes())
    finally:
        if rag is not None: rag.close()
        index.close()
        client.delete_collection(collection.name)
