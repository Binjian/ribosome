import json
import traceback
import sys
import re

def validate_notebook(nb_path):
    with open(nb_path, 'r', encoding='utf-8') as f:
        nb = json.load(f)
    
    namespace = {}
    cell_idx = 0
    for cell in nb.get('cells', []):
        if cell.get('cell_type') == 'code':
            cell_idx += 1
            code_lines = cell.get('source', [])
            if not code_lines:
                continue
            
            # Filter out magics and shell escapes
            filtered_lines = []
            for line in code_lines:
                stripped = line.strip()
                if not (stripped.startswith('%') or stripped.startswith('!')):
                    filtered_lines.append(line)
            
            code = "".join(filtered_lines)
            try:
                # Execute the code in the shared namespace
                exec(code, namespace)
            except Exception as e:
                # Get a short traceback summary
                tb = traceback.format_exc().splitlines()
                summary = tb[-1] if tb else str(e)
                return f"FAIL: Cell {cell_idx} failed. {summary}"
    
    return "PASS"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python validate_nb.py <notebook_path>")
        sys.exit(1)
    print(validate_notebook(sys.argv[1]))
