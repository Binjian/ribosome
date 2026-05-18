import json
import sys
import traceback

def validate_notebook(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        nb = json.load(f)
    
    namespace = {}
    code_cell_num = 0
    for cell in nb.get('cells', []):
        if cell.get('cell_type') == 'code':
            code_cell_num += 1
            source = "".join(cell.get('source', []))
            lines = []
            for line in source.splitlines():
                stripped = line.strip()
                if not stripped.startswith('%') and not stripped.startswith('!'):
                    lines.append(line)
            code = "\n".join(lines)
            if not code.strip():
                continue
            
            try:
                exec(code, namespace)
            except Exception:
                print(f"FAIL: Cell {code_cell_num}")
                exc_type, exc_value, exc_traceback = sys.exc_info()
                # Get the last line of the exception message
                msg = traceback.format_exception_only(exc_type, exc_value)[-1].strip()
                print(msg)
                return
    print("PASS")

if __name__ == "__main__":
    validate_notebook(sys.argv[1])
