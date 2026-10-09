"""Idempotently install offline replacement/identity notebook regressions."""
import json
from pathlib import Path
SOURCE = '''# | hide
def test_embedding_replacement_identity_and_failure_contract():
    original = {'summary': '文档', 'blocks': [
        {'t': 'Section', 's': 'same'}, {'t': 'Section', 's': 'same'}],
        'a/~b': {'t': 'Table', 's': '表格'},
        'business': {'i': 'semantic', 'm': {'meaning': 'keep'}}}
    collection = FakeCollection()
    def execute(tree, action=None, target=None):
        target = collection if target is None else target
        client = FakeAsyncClient([{'embeddings': [[1, 2]], 'model': 'reported'}] * 4)
        result = json.loads(run_async(embed(json.dumps(tree), Path('sample.md'),
            {'Section', 'Table'}, None, target, client, 'requested', action)))
        return result, client
    result, client = execute(original)
    test_eq(len(collection.rows), 4)
    test_eq(len(client.calls), 4)
    assert result['blocks'][0]['i'] != result['blocks'][1]['i']
    test_eq(result['blocks'][0]['m']['node_path'], '$/blocks/0')
    test_eq(result['a/~b']['m']['node_path'], '$/a~1~0b')
    test_eq(result['m']['embed_model'], 'reported')
    test_eq(_semantic_payload(result), original)
    test_eq(result['i'], hashlib.sha256(('sample.md'+_canonical_json(original)).encode()).hexdigest())
    repeated, _ = execute(result)
    test_eq(repeated, result)
    test_eq(len(collection.rows), 4)
    changed = {'summary': 'replacement', 'blocks': [{'t':'Section','s':'new'}]}
    replacement, _ = execute(changed)
    test_eq(set(collection.rows), {replacement['i'], replacement['blocks'][0]['i']})
    test_eq(collection.operations[-1][0], 'delete')

    seen = []
    def transform(node):
        seen.append(type(node).__name__)
        if isinstance(node, dict):
            return {**node, 'seen': True}
        return node
    transformed, _ = execute(changed, transform)
    test_eq(seen, ['dict','list','dict'])
    test_eq(transformed['i'], hashlib.sha256(('sample.md'+_canonical_json(
        {'summary':'replacement','seen':True,'blocks':[{'t':'Section','s':'new','seen':True}]})).encode()).hexdigest())
    untouched = FakeCollection()
    test_fail(lambda: execute(changed, lambda node: [] if isinstance(node,dict) else node, untouched),
              contains='action must preserve dict')
    test_eq(untouched.operations, [])
    empty, empty_client = execute({'summary':'  ','blocks':[]}, target=FakeCollection())
    assert 'i' not in empty and 'm' not in empty
    test_eq(empty_client.calls, [])

    for stage in ('get','upsert','delete','provider'):
        failing = FakeCollection()
        execute(original, target=failing)
        old_ids = set(failing.rows)
        original_method = getattr(failing, stage) if stage != 'provider' else None
        def fail(*args, **kwargs):
            raise RuntimeError('injected failure')
        if original_method is not None:
            setattr(failing, stage, fail)
            test_fail(lambda: execute(changed, target=failing), contains='retry to converge')
            setattr(failing, stage, original_method)
        else:
            failing_client = FakeAsyncClient([{'embeddings': []}, {'embeddings': []}])
            test_fail(lambda: run_async(embed(json.dumps(changed), Path('sample.md'),
                {'Section'}, None, failing, failing_client)), contains='retry to converge')
        assert old_ids <= set(failing.rows)
        recovered, _ = execute(changed, target=failing)
        test_eq(set(failing.rows), {recovered['i'], recovered['blocks'][0]['i']})


test_embedding_replacement_identity_and_failure_contract()
'''
def main():
    p=Path(__file__).resolve().parents[1]/'nbs/01.core.dom.embedding.ipynb'
    n=json.loads(p.read_text())
    cell=next((c for c in n['cells'] if c.get('id')=='embedding-replacement-regressions'),None)
    if cell is None:
        cell={'cell_type':'code','id':'embedding-replacement-regressions','metadata':{},
              'execution_count':None,'outputs':[]}
        n['cells'].insert(-1,cell)
    cell['source']=SOURCE.splitlines(keepends=True)
    p.write_text(json.dumps(n,ensure_ascii=False,indent=1)+'\n')
if __name__=='__main__':main()
