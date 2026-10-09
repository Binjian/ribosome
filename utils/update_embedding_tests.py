"""Idempotently migrate embedding regression fakes and metadata assertions."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
FAKE = '''class FakeCollection:
    def __init__(self):
        self.rows = {}
        self.operations = []

    @property
    def records(self):
        return list(self.rows.values())

    def upsert(self, ids, metadatas, embeddings, documents):
        self.operations.append(('upsert', list(ids)))
        for i, identifier in enumerate(ids):
            self.rows[identifier] = dict(ids=[identifier], metadatas=[metadatas[i]],
                embeddings=embeddings[i:i+1], documents=[documents[i]])

    def get(self, where=None, include=None):
        self.operations.append(('get', where))
        return {'ids': [identifier for identifier, row in self.rows.items()
            if not where or all(row['metadatas'][0].get(k) == v for k, v in where.items())]}

    def delete(self, ids):
        self.operations.append(('delete', list(ids)))
        for identifier in ids:
            self.rows.pop(identifier, None)
'''
def update(s):
    start=s.index('class FakeCollection:')
    end=s.find('\n\n', s.index('    def upsert(', start))
    if 'self.rows = {}' not in s[start:end]:
        s=s[:start]+FAKE+s[end:]
    s=s.replace('"doc_id"', '"document_id"')
    s=s.replace('"node_path": "root-id",', '"node_path": "$",')
    s=s.replace('        "file_path": "",\n','')
    s=s.replace('result["m"]["embed_model"], "seed-model"', 'result["m"]["embed_model"], "ast-model"')
    s=s.replace('result["m"]["node_path"], result["i"]', 'result["m"]["node_path"], "$"')
    s=s.replace('collection.records[1]["metadatas"][0]["node_path"], result["i"]', 'collection.records[1]["metadatas"][0]["node_path"], "$/children/0"')
    s=s.replace('f"ast-id/{result[\'i\']}"', '"$/children/0"')
    s=s.replace('f"ast-id/{outer_i}/{inner_i}"', '"$/children/0/children/0"')
    s=s.replace('f"ast-id/{outer_i}"', '"$/children/0"')
    s=s.replace('collection.records[2]["metadatas"][0]["node_path"], "ast-id"', 'collection.records[2]["metadatas"][0]["node_path"], "$"')
    s=s.replace('"embed_model": dom.ollama_model', '"embed_model": "fake-embed-model"')
    s=s.replace('"node_path": root_id', '"node_path": "$"')
    s=s.replace('f"{root_id}/{section_one_id}"', '"$/blocks/0/c/1/c/0"')
    s=s.replace('section_one_metadata["node_path"], root_id', 'section_one_metadata["node_path"], "$/blocks/0"')
    marker = 'def test_embed_node_dict_response_without_model_falls_back_to_requested():'
    if marker in s:
        before, after = s.split(marker, 1)
        after = after.replace('result["m"]["embed_model"], "ast-model"', 'result["m"]["embed_model"], "seed-model"', 1)
        s = before + marker + after
    s=s.replace('# the ancestor path stack must be restored after the walk (push/pop discipline)', '# the compatibility ancestor seed remains unchanged')
    s=s.replace('# the table child saw the seeded ast id and the root as its ancestors', '# structural paths are independent of ancestor IDs')
    s=s.replace('# node_path holds the true ancestor chain, not a completion-ordered list', '# node_path records dict keys and list indexes')
    s=s.replace('# node rows carry provenance metadata: ancestors-only node_path and the source file', '# node rows carry structural paths and exact source provenance')
    return s

def main():
    for name in ('tests/test_embedding_fastcore.py','tests/test_dom_behavior.py'):
        p=ROOT/name;p.write_text(update(p.read_text()))
    p=ROOT/'nbs/01.core.dom.embedding.ipynb';n=json.loads(p.read_text())
    for c in n['cells']:
        s=''.join(c['source'])
        if c['cell_type']=='code' and '# | hide' in s:
            if 'class FakeCollection:' in s:
                # Cell ends at FakeCollection; append delimiter to reuse replacement.
                s=update(s+'\n\n').rstrip()+'\n'
            else:
                # Assertions use same migration, without a fake declaration.
                prefix=FAKE+'\n\n';s=update(prefix+s)[len(prefix):]
            c['source']=s.splitlines(keepends=True)
    p.write_text(json.dumps(n,ensure_ascii=False,indent=1)+'\n')
if __name__=='__main__':main()
