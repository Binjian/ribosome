import json
import sys

def print_cell(path, target_idx):
    with open(path, 'r', encoding='utf-8') as f:
        nb = json.load(f)
    
    code_cell_idx = 0
    for cell in nb['cells']:
        if cell['cell_type'] == 'code':
            code_cell_idx += 1
            if code_cell_idx == target_idx:
                print(f"--- Cell {code_cell_idx} ---")
                print("".join(cell['source']))
                print("--- End Cell ---")
                return

print_cell(sys.argv[1], 13)
