import json
import re
import sys
import traceback

def run_notebook(path):
    print(f"Validating {path}...")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            nb = json.load(f)
    except Exception as e:
        print(f"FAILED to parse JSON: {e}")
        return False

    namespace = {}
    cell_idx = 0
    for cell in nb.get('cells', []):
        if cell.get('cell_type') == 'code':
            cell_idx += 1
            code_lines = cell.get('source', [])
            cleaned_lines = []
            for line in code_lines:
                # Strip magics and shell escapes
                if line.strip().startswith('%') or line.strip().startswith('!'):
                    continue
                cleaned_lines.append(line)
            
            code = "".join(cleaned_lines)
            if not code.strip():
                continue
                
            try:
                exec(code, namespace)
            except Exception:
                print(f"FAILED at cell {cell_idx}")
                print(traceback.format_exc(limit=3))
                return False
    
    print("PASSED")
    return True

if __name__ == "__main__":
    results = {}
    for nb_path in sys.argv[1:]:
        results[nb_path] = run_notebook(nb_path)
    
    if all(results.values()):
        sys.exit(0)
    else:
        sys.exit(1)
