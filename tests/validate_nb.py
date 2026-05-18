import json
import traceback
import sys

def validate_notebook(nb_path):
    try:
        with open(nb_path, 'r', encoding='utf-8') as f:
            nb = json.load(f)
    except Exception as e:
        print(f"Failed to load notebook JSON: {e}")
        return

    namespace = {}
    cell_idx = 0
    for cell in nb.get('cells', []):
        if cell.get('cell_type') == 'code':
            cell_idx += 1
            source = "".join(cell.get('source', []))
            
            # Filter out magics and shell escapes
            lines = source.splitlines()
            filtered_lines = []
            for line in lines:
                strip_line = line.strip()
                if strip_line.startswith('%') or strip_line.startswith('!'):
                    continue
                filtered_lines.append(line)
            
            code = "\n".join(filtered_lines)
            if not code.strip():
                continue
                
            try:
                exec(code, namespace)
            except Exception:
                print(f"FAIL")
                print(f"Cell: {cell_idx}")
                print(traceback.format_exc(limit=1).splitlines()[-1])
                return

    print("PASS")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python validate_nb.py <notebook_path>")
    else:
        validate_notebook(sys.argv[1])
