"""Make notebook function tests regenerate their own document artifacts."""
import json
from pathlib import Path

HELPER = '''# | hide
# | notest
def _fresh_function_test_dom(asset_name):
    """Remove derived caches before setup so each manual test starts fresh."""
    project_root = get_project_root()
    md_file = ((project_root / 'assets' / asset_name / f'{asset_name}.md')
               if project_root is not None else Path('assets') / asset_name / f'{asset_name}.md')
    if not md_file.exists():
        md_file = Path('../assets') / asset_name / f'{asset_name}.md'
    assert md_file.is_file(), f'Markdown source missing: {md_file}'
    # Clear every registered artifact, including downstream semantics/embedding
    # caches that would otherwise describe the previous AST.
    for suffix in DOMClass._ARTIFACT_SUFFIXES.values():
        artifact = md_file.with_name(f'{md_file.stem}{suffix}')
        artifact.unlink(missing_ok=True)
    dom = DOMClass(md_file)
    dom.setup()
    assert dom.ast_json_file and dom.ast_json_file.exists(), 'AST was not regenerated'
    assert dom.ast_json and json.loads(dom.ast_json).get('blocks'), 'AST blocks missing'
    assert not dom.semantics_json and not dom.embed_json, 'Cached downstream artifacts loaded'
    print(f'Regenerated AST: {dom.ast_json_file}')
    return dom
'''

def main():
    path=Path(__file__).resolve().parents[1]/'nbs/01.core.dom.model.ipynb'
    notebook=json.loads(path.read_text())
    helper=next((c for c in notebook['cells'] if c.get('id')=='fresh-function-test-dom'),None)
    if helper is None:
        heading=next(i for i,c in enumerate(notebook['cells']) if ''.join(c['source']).strip()=='# DomClass Function Tests')
        helper={'cell_type':'code','id':'fresh-function-test-dom','execution_count':None,'metadata':{},'outputs':[]}
        notebook['cells'].insert(heading+1,helper)
    helper['source']=HELPER.splitlines(keepends=True)
    for cell_id,asset,kind in [('97bcb2cb','SN024002','reorg'),('ff616489','SX022001','reorg'),
                              ('e41bf743','SN024002','textualize'),('9727c25b','SX022001','textualize')]:
        async_prefix='async ' if kind=='textualize' else ''
        name=f'test_{kind}_with_{asset.lower()}_md'
        s=f'# | hide\n# | notest\n{async_prefix}def {name}():\n    dom = _fresh_function_test_dom("{asset}")\n'
        if kind=='textualize':
            s+='''    ast_before = dom.ast_json_file.read_bytes()
    print('Starting textualization...')
    await dom.summary_nodewline_main()
    assert dom.semantics_json and dom.semantics_json_file, 'Semantics were not generated'
    # Always replace the semantics artifact, regardless of previous runs.
    dom.semantics_json_file.write_text(dom.semantics_json, encoding='utf-8')
    test_eq(json.loads(dom.semantics_json_file.read_text(encoding='utf-8')),
            json.loads(dom.semantics_json))
    test_eq(dom.ast_json_file.read_bytes(), ast_before)
    print(f'Regenerated semantics: {dom.semantics_json_file}')
'''
        s+=f'\n\n{"await " if kind=="textualize" else ""}{name}()\n'
        next(c for c in notebook['cells'] if c.get('id')==cell_id)['source']=s.splitlines(keepends=True)
    path.write_text(json.dumps(notebook,ensure_ascii=False,indent=1)+'\n')

if __name__=='__main__':main()
