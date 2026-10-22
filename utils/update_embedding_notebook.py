"""Apply the embedding correctness implementation to its nbdev source notebook.

Run from any directory with Python 3. This script is idempotent and preserves
cell IDs and metadata; export separately with nbdev-export afterwards.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPERS = '''# | export
def _canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ';'))


def _semantic_payload(value):
    """Strip only recognizable pipeline annotations; retain business i/m fields."""
    if isinstance(value, dict):
        metadata = value.get('m')
        owned = (isinstance(value.get('i'), str)
                 and re.fullmatch(r'[0-9a-f]{64}', value['i']) is not None
                 and isinstance(metadata, dict)
                 and {'embed_model', 'node_type', 'node_path'} <= metadata.keys()
                 and ('document_id' in metadata or 'doc_id' in metadata)
                 and (isinstance(value.get('t'), str) or 'summary' in value or 'blocks' in value
                      or (metadata.get('node_type') == 'Document'
                          and metadata.get('document_id', metadata.get('doc_id')) == value['i']
                          and metadata.get('node_path') in ('$', value['i']))))
        return {k: _semantic_payload(v) for k, v in value.items()
                if not (owned and k in ('i', 'm'))}
    if isinstance(value, list):
        return [_semantic_payload(v) for v in value]
    return value


def _child_path(parent, key_or_index):
    return parent + '/' + str(key_or_index).replace('~', '~0').replace('/', '~1')


def _response_model(response, requested_model):
    value = response.get('model') if isinstance(response, dict) else getattr(response, 'model', None)
    return value if isinstance(value, str) and value else requested_model


def _validate_embed_types(embed_types):
    if isinstance(embed_types, str) or not all(isinstance(t, str) for t in embed_types):
        raise ValueError('embed_types must contain strings')


def _transform_embedding_tree(value, action, path='$'):
    """Apply action once per dict/list (including list containers), before hashing.

    Scalar fields are preserved and are not callback targets, matching the public
    embedding callback contract. Replacements must retain their container category.
    """
    if isinstance(value, (dict, list)):
        replacement = action(value) if action is not None else value
        if not isinstance(replacement, type(value)):
            raise ValueError(f'action must preserve {type(value).__name__} at {path}')
        if isinstance(replacement, dict):
            return {k: _transform_embedding_tree(v, action, _child_path(path, k))
                    for k, v in replacement.items()}
        return [_transform_embedding_tree(v, action, _child_path(path, i))
                for i, v in enumerate(replacement)]
    return value


def _node_id(node, doc_id, structural_path):
    framed = {'document_id': doc_id, 'node_path': structural_path,
              'semantic_node': _semantic_payload(node)}
    return hashlib.sha256(_canonical_json(framed).encode('utf-8')).hexdigest()


async def _persist_embedding(node, node_id, doc_id, structural_path, file_path,
                             collection, llm_client, model, written, root=False):
    text = node.get('s') or (node.get('summary', '') if root else '')
    if not isinstance(text, str):
        raise ValueError(f'Embedding summary must be a string at {structural_path}')
    if not text.strip():
        return node
    try:
        vector, response = await get_embeddings_with_retry(
            text, f'{file_path} node {node_id} at {structural_path}', llm_client, model)
        if not vector or not np.isfinite(vector).all():
            raise ValueError('empty or non-finite embedding')
        metadata = {'embed_model': _response_model(response, model),
                    'node_type': node.get('t') or ('Document' if root else ''),
                    'node_path': structural_path}
        if doc_id:
            metadata['document_id'] = doc_id
        if file_path:
            metadata['file_path'] = file_path
        collection.upsert(ids=[node_id], metadatas=[metadata],
                          embeddings=np.asarray([vector], dtype=np.float32), documents=[text])
    except Exception as exc:
        raise RuntimeError(f'Embedding failed for document {doc_id}, source {file_path}, '
                           f'node {node_id} at {structural_path}: {exc}') from exc
    written.add(node_id)
    node['i'], node['m'] = node_id, metadata
    return node


async def _embed_walk(node, doc_id, structural_path, file_path, collection,
                      embed_types, llm_client, model, written, skip_self_embed=False):
    # Hash before descendants acquire row annotations. Insertion remains child-first.
    node_id = _node_id(node, doc_id, structural_path) if isinstance(node, dict) else None
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if isinstance(value, (dict, list)):
                node[key] = await _embed_walk(value, doc_id, _child_path(structural_path, key),
                    file_path, collection, embed_types, llm_client, model, written)
        if not skip_self_embed and node.get('t') in embed_types:
            await _persist_embedding(node, node_id, doc_id, structural_path, file_path,
                                     collection, llm_client, model, written)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            if isinstance(value, (dict, list)):
                node[i] = await _embed_walk(value, doc_id, _child_path(structural_path, i),
                    file_path, collection, embed_types, llm_client, model, written)
    return node


def _wrapper_context(cur_object_path):
    # Legacy argument remains an ancestor-ID sequence, never a structural path.
    # First ancestor supplies document identity; additional ancestors are framed
    # into an explicit compatibility namespace. The sequence is never modified.
    doc_id = cur_object_path[0] if cur_object_path else ''
    path = '$' if len(cur_object_path) <= 1 else '$/legacy/' + hashlib.sha256(
        _canonical_json(cur_object_path).encode('utf-8')).hexdigest()
    return doc_id, path
'''
EMBED = '''# | export
async def embed(semantics_json: str, file_path: Path, embed_types: set[str],
                db_client: Optional[ClientAPI], collection: Collection,
                llm_client: AsyncClient, model: str='bge-m3',
                action: Optional[Callable]=None) -> str:
    """Replace this source's rows after a complete successful embedding walk.

    Upserts are not transactional: failure may leave additional new rows, but
    stale deletion happens only after all writes succeed. Retrying converges.
    Pre-provenance rows require a separate migration or collection rebuild.
    """
    # db_client is accepted for caller compatibility but unused.
    _validate_embed_types(embed_types)
    ast = json.loads(semantics_json)
    if not isinstance(ast, dict):
        raise ValueError('AST must be a dictionary')
    ast = _transform_embedding_tree(_semantic_payload(ast), action)
    source = str(file_path)
    doc_id = hashlib.sha256((source + _canonical_json(ast)).encode('utf-8')).hexdigest()
    written = set()
    try:
        old_ids = set(collection.get(where={'file_path': source}, include=[])['ids'])
        await _persist_embedding(ast, doc_id, doc_id, '$', source, collection,
                                 llm_client, model, written, root=True)
        await _embed_walk(ast, doc_id, '$', source, collection, embed_types,
                          llm_client, model, written, skip_self_embed=True)
        stale = old_ids - written
        if stale:
            collection.delete(ids=sorted(stale))
    except Exception as exc:
        raise RuntimeError(f'Embedding replacement failed for document {doc_id}, source {source}; '
                           f'{len(written)} rows upserted; retry to converge: {exc}') from exc
    return json.dumps(ast, ensure_ascii=False)
'''
NODE = '''# | export
async def embed_node(node: dict, collection: Collection, cur_object_path: list[str],
                     embed_types: set[str], llm_client: AsyncClient, model: str,
                     file_path: str='') -> dict:
    """Compatibility wrapper: ancestor IDs seed identity; standalone path is $.

    Missing source provenance is omitted. Explicit structural context is carried
    by the private pipeline helpers. Existing input IDs are never trusted.
    """
    _validate_embed_types(embed_types)
    if node.get('t') not in embed_types:
        raise ValueError(f"Node type {node.get('t')} is not in embed_types: {embed_types}")
    node = _semantic_payload(node)
    doc_id, path = _wrapper_context(cur_object_path)
    return await _persist_embedding(node, _node_id(node, doc_id, path), doc_id, path,
                                   str(file_path), collection, llm_client, model, set())
'''
WALK = '''# | export
async def embed_walk_node(node: dict | list, cur_object_path: list[str], collection: Collection,
                          embed_types: set[str], llm_client: AsyncClient, model: str='bge-m3',
                          action: Optional[Callable]=None, file_path: str='',
                          skip_self_embed: bool=False) -> dict | list:
    """Compatibility walk with immutable ancestor seed and standalone path $.

    Action visits dict/list containers once, including list-valued properties;
    scalar fields remain unchanged. All transforms finish before any API call.
    """
    _validate_embed_types(embed_types)
    if not isinstance(node, (dict, list)):
        raise ValueError('node must be a dict or list')
    node = _transform_embedding_tree(_semantic_payload(node), action)
    doc_id, path = _wrapper_context(cur_object_path)
    return await _embed_walk(node, doc_id, path, str(file_path), collection,
                            embed_types, llm_client, model, set(), skip_self_embed)
'''


def main():
    path = ROOT / 'nbs/01.core.dom.embedding.natural_text.ipynb'
    notebook = json.loads(path.read_text())
    replacements = {'7a1aeb88': HELPERS + '\n' + EMBED.removeprefix('# | export\n'),
                    '076222a3': NODE, '3850676e': WALK}
    for cell in notebook['cells']:
        if cell.get('id') in replacements:
            cell['source'] = replacements[cell['id']].splitlines(keepends=True)
    path.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + '\n')


if __name__ == '__main__':
    main()
