# RC5 Grammar Update Summary

## Overview
Updated the Siasun robot interpreter to support RC5/Job.g4 grammar format. The grammar conversion from ANTLR4 to tree-sitter was completed earlier, and now the Python interpreter has been updated to match.

## Key Syntax Changes (Old → RC5)

### Variables
- **Old**: `I1`, `R1`, `P1`, `J1`
- **RC5**: `I[1]`, `R[1]`, `P[1]`, `J[1]`
- Uses bracket notation `[index]` instead of concatenated numbers

### I/O Operations
- **Old**: `OT#1`, `IN#1`, `GO#1`, `GI#1`
- **RC5**: `DO[1]`, `DI[1]`, `GO[1]`, `GI[1]`
- OUT statement format: `OUT DO[1] 1` (instead of `OUT OT#1=ON`)

### Motion Parameters
- **Old**: `VJ=50`, `VL=100`, `ACC=80` (no spaces)
- **RC5**: `V = 50`, `ACC = 80` (with spaces, unified V parameter)
- RC5 uses `V` for both joint and linear velocity (not VJ/VL)

### Frame Commands
- **Old**: `TF #1`, `UF #1`
- **RC5**: `SETFRAME TF 1`, `SETFRAME UF 1`
- Uses SETFRAME keyword with space-separated parameters

### Labels
- **Old**: `L10:`, `GOTO L10`
- **RC5**: `LABEL "L10"`, `GOTO "L10"`
- Labels are string literals in quotes

### Arithmetic Expressions
- **Old**: `ADD I1 1`, `MUL R3 R2 I1`
- **RC5**: `I[1] = I[1] + 1`, `R[3] = R[2] * I[1]`
- Uses inline expressions with operators

### Position Assignment
- **Old**: `SET P1 LPOS` (sets position to current robot location)
- **RC5**: `SET P[1] P[1]` or `SET P[1] P[2]`
- LPOS keyword removed, positions set to other positions

## Updated Files

### 1. siasun_interpreter.py
**Location**: `/Users/x/devel/cell/ribosome/scripts/siasun_interpreter.py`

**Changes**:
- Updated variable initialization comments to reflect RC5 format
- Modified `_resolve_value()` to parse `I[1]` bracket notation using regex
- Updated `_parse_motion_params()` to handle space-separated parameters
- Modified `_exec_movj()` and `_exec_movl()` to use `V` instead of `VJ`/`VL`
- Updated `_exec_out()` to parse RC5 IO format `DO[1] 1`
- Modified `_exec_set()` to use regex for bracket notation matching
- Updated `_exec_add()`, `_exec_sub()`, `_exec_mul()` for bracket variables
- Replaced `_exec_macro()` with `_exec_setframe_rc5()` for SETFRAME parsing
- Added new RC5 handler methods:
  - `_exec_motion_rc5()`
  - `_exec_io_rc5()`
  - `_exec_setframe_rc5()`
  - `_exec_set_rc5()`
  - `_exec_cop_rc5()`
  - `_exec_control_rc5()`
- Updated dispatcher to handle RC5 node types:
  - `instStart`, `instEnd`, `inst`, `opExp`, `baseInst`
  - `moveInst`, `ioInst`, `setFrameInst`, `setInst`
  - `copInst`, `controlInst`
- Updated test program to use RC5 syntax

### 2. tree-sitter-siasun/pyproject.toml
**Location**: `/Users/x/devel/cell/ribosome/scripts/tree-sitter-siasun/pyproject.toml`

**Changes**:
- Removed empty `Funding = ""` line that caused pip installation error

## Tree-Sitter Grammar Structure

### Main Rules
- `prog`: Top-level program structure (`instStart` → `inst*` → `instEnd`)
- `inst`: Individual instructions (operations + newline, or comments)
- `opExp`: Operation expressions (computation, IO, base, move, control)

### Variable Rules
- `iVar`: Integer variables `I[index]`
- `hVar`: Handle variables `H[index]`
- `bVar`: Byte variables `B[index]`
- `floatVar`: Real variables `R[index]`
- `strVar`: String variables `S[index]`
- `positionVar`: Position variables `P[index]` or `PR[index]`

### Instruction Categories
- `moveInst`: Motion commands (MOVJ, MOVL, MOVC, MOVS)
- `ioInst`: I/O operations (OUT, IN, ANIN, ANOUT)
- `baseInst`: Basic operations (SET, SETFRAME, CLEAR, string ops)
- `copInst`: Computation operations (arithmetic, logic, math functions)
- `controlInst`: Control flow (IF, FOR, WHILE, SWITCH, GOTO, LABEL)

## Build & Test

### Rebuild Parser
```bash
cd /Users/x/devel/cell/ribosome/scripts/tree-sitter-siasun
tree-sitter generate
pip install --force-reinstall -e .
cd ..
python build_parser.py
```

### Test Interpreter
```bash
cd /Users/x/devel/cell/ribosome/scripts
python siasun_interpreter.py tests/pick_and_place.siasun
python siasun_interpreter.py tests/loop_counter.siasun
```

## Test Results

All 13 test files successfully parse and execute with RC5 format:
- ✓ pick_and_place.siasun - Basic motion and I/O
- ✓ loop_counter.siasun - Variables, loops, conditionals
- ✓ move_x_axis_100mm_offset.siasun - Motion with parameters
- ✓ move_x_axis_100mm_position.siasun - Position-based motion
- ✓ coordinate_systems.siasun - Frame commands
- ✓ dispensing_path.siasun - Complex path operations
- ✓ circular_arc_weld.siasun - MOVC circular motion
- ✓ multi_point_path.siasun - Multiple waypoints
- ✓ spiral_path.siasun - Advanced motion patterns
- ✓ io_synchronization.siasun - I/O operations
- ✓ error_handling.siasun - Error control
- ✓ tool_change.siasun - Tool frame changes
- ✓ palletizing_3x3.siasun - Nested loops

## Status

**Completed**:
- ✅ Tree-sitter grammar.js conversion from Job.g4
- ✅ Parser generation and Python bindings
- ✅ 13 test files converted to RC5 format
- ✅ Interpreter updated for RC5 syntax
- ✅ Variable parsing (bracket notation)
- ✅ I/O operations (DO[1] format)
- ✅ Motion parameter parsing (spaced format)
- ✅ Frame commands (SETFRAME)
- ✅ Basic control flow recognition
- ✅ All test files parse and execute

**Partial/TODO**:
- ⚠️ Arithmetic expressions logged but not fully executed
  - Currently showing as "Control:" or "Operation:" messages
  - Need to implement expression evaluation for `I[1] = I[1] + 1` etc.
- ⚠️ Control flow (IF, GOTO, LABEL) logged but not implemented
  - Control flow logic needs actual branching implementation
  - Labels need to be indexed for jump operations

## Notes

1. The interpreter now successfully parses RC5 format programs with the new tree-sitter grammar
2. Basic operations (motion, I/O, SET, SETFRAME) work correctly
3. Arithmetic expressions and control flow are recognized but need full implementation
4. The grammar handles all RC5 syntax correctly with proper AST generation
5. The Python bindings use the new tree-sitter 0.24+ API

## References

- Original grammar: `scripts/rc5/Antlr4/Job.g4` (RC 5.0, modified 2025.9.10)
- Tree-sitter grammar: `scripts/tree-sitter-siasun/grammar.js`
- Interpreter: `scripts/siasun_interpreter.py`
- Test files: `scripts/tests/*.siasun`
