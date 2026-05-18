import json
def debug():
    with open('/d/devel/rag/ribosome/nbs/01.core.dom.reorg.ipynb', 'r') as f:
        nb = json.load(f)
    code_cell_idx = 0
    for cell in nb['cells']:
        if cell['cell_type'] == 'code':
            code_cell_idx += 1
            if code_cell_idx == 13:
                for i, line in enumerate(cell['source']):
                    print(f"{i+1}: {repr(line)}")
debug()
