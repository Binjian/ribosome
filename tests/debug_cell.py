import json, sys, re, traceback, asyncio
from typing import *

def validate(nb_path):
    with open(nb_path, 'r', encoding='utf-8') as f:
        nb = json.load(f)
    
    namespace = {}
    cell_idx = 0
    for cell in nb['cells']:
        if cell['cell_type'] == 'code':
            cell_idx += 1
            source = "".join(cell['source'])
            lines = []
            for line in source.splitlines():
                if line.strip().startswith('%') or line.strip().startswith('!'):
                    continue
                lines.append(line)
            code = "\n".join(lines)
            
            if not code.strip():
                continue
                
            if cell_idx == 10:
                print(f"--- Code for Cell {cell_idx} ---")
                print(code)
                print("--- End Code ---")

            try:
                # To handle 'await' at top level if any, though usually cells are sync or use asyncio.run
                # But here map_tree_async is defined, then presumably tested.
                exec(code, namespace)
            except Exception:
                print(f"FAILED: Cell {cell_idx}")
                traceback.print_exc(limit=5)
                sys.exit(1)
    print("PASSED")

if __name__ == "__main__":
    validate(sys.argv[1])
