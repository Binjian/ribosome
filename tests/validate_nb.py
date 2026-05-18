import json, re, sys

def validate_nb(path):
    with open(path, 'r', encoding='utf-8') as f:
        nb = json.load(f)
    
    code_cells = [cell for cell in nb['cells'] if cell['cell_type'] == 'code']
    ns = {}
    
    for i, cell in enumerate(code_cells):
        code = "".join(cell['source'])
        # Strip magics and shell escapes
        lines = []
        for line in code.splitlines():
            if line.strip().startswith('%') or line.strip().startswith('!'):
                continue
            lines.append(line)
        clean_code = "\n".join(lines)
        
        try:
            exec(clean_code, ns)
        except Exception as e:
            import traceback
            print(f"FAIL: Cell {i}")
            print(traceback.format_exc(limit=3))
            sys.exit(1)
    
    print("PASS")

if __name__ == "__main__":
    validate_nb(sys.argv[1])
