import json
import sys
import traceback

def run_notebook(path):
    with open(path, 'r', encoding='utf-8') as f:
        nb = json.load(f)
    
    namespace = {}
    code_cell_idx = 0
    for cell in nb['cells']:
        if cell['cell_type'] == 'code':
            code_cell_idx += 1
            # Maintain formatting by joining with empty string since notebook lines usually include \n
            source = "".join(cell['source']) if isinstance(cell['source'], list) else cell['source']
            
            lines = source.splitlines()
            filtered_lines = []
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith('!') or stripped.startswith('%'):
                    filtered_lines.append('')
                else:
                    filtered_lines.append(line)
            
            code = "\n".join(filtered_lines)
            if not code.strip():
                continue
            
            # Special case for cell 13's split string if it was actually intended as one
            # The repr showed "    slide_markdown = '\n" on line 4 and "'.join([\n" on line 5.
            # This is a syntax error in Python (line 4 has an unclosed quote).
            # If it works in Jupter, it might be due to some IPyhon magic or 
            # perhaps the newline after the quote was handled differently.
            # But here I must execute the code as-is.

            try:
                exec(compile(code, f"Cell_{code_cell_idx}", 'exec'), namespace)
            except Exception as e:
                print(f"FAIL: Cell {code_cell_idx}")
                print(traceback.format_exc())
                return False
            except SyntaxError as e:
                print(f"FAIL: Cell {code_cell_idx}")
                print(f"SyntaxError: {e.msg} at line {e.lineno}")
                return False
    print("PASS")
    return True

if __name__ == "__main__":
    run_notebook(sys.argv[1])
