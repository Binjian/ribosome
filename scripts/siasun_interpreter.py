"""
Siasun Virtual Interpreter

A virtual interpreter for Siasun Robot Language that uses tree-sitter
for parsing and simulates robot execution for testing purposes.

Usage:
    from siasun_interpreter import SiasunInterpreter
    
    interpreter = SiasunInterpreter()
    interpreter.load_file("path/to/program.siasun")
    interpreter.run()
"""

import ctypes
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Any
from tree_sitter import Language, Parser


# ============================================================================
# Robot State Classes
# ============================================================================

@dataclass
class Position:
    """6-DOF robot position (X, Y, Z, Rx, Ry, Rz)"""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0
    
    def __add__(self, other: "Position") -> "Position":
        return Position(
            self.x + other.x, self.y + other.y, self.z + other.z,
            self.rx + other.rx, self.ry + other.ry, self.rz + other.rz
        )
    
    def __sub__(self, other: "Position") -> "Position":
        return Position(
            self.x - other.x, self.y - other.y, self.z - other.z,
            self.rx - other.rx, self.ry - other.ry, self.rz - other.rz
        )
    
    def to_array(self) -> np.ndarray:
        return np.array([self.x, self.y, self.z, self.rx, self.ry, self.rz])
    
    @classmethod
    def from_array(cls, arr: np.ndarray) -> "Position":
        return cls(*arr[:6])
    
    def __repr__(self) -> str:
        return f"Pos({self.x:.2f}, {self.y:.2f}, {self.z:.2f}, {self.rx:.2f}, {self.ry:.2f}, {self.rz:.2f})"


@dataclass
class JointAngles:
    """Robot joint angles (J1-J6)"""
    j1: float = 0.0
    j2: float = 0.0
    j3: float = 0.0
    j4: float = 0.0
    j5: float = 0.0
    j6: float = 0.0
    
    def to_array(self) -> np.ndarray:
        return np.array([self.j1, self.j2, self.j3, self.j4, self.j5, self.j6])
    
    def __repr__(self) -> str:
        return f"Joints({self.j1:.2f}, {self.j2:.2f}, {self.j3:.2f}, {self.j4:.2f}, {self.j5:.2f}, {self.j6:.2f})"


@dataclass
class RobotState:
    """Complete robot state"""
    # Current position (Cartesian)
    position: Position = field(default_factory=Position)
    # Current joint angles
    joints: JointAngles = field(default_factory=JointAngles)
    # Tool frame number
    tool_frame: int = 0
    # User frame number
    user_frame: int = 0
    # Current velocity
    velocity: float = 0.0
    # Running state
    is_running: bool = False
    # Error state
    has_error: bool = False
    error_message: str = ""


# ============================================================================
# Siasun Interpreter
# ============================================================================

class SiasunInterpreter:
    """Virtual interpreter for Siasun Robot Language"""
    
    def __init__(self, library_path: Optional[str] = None):
        """
        Initialize the interpreter.
        
        Args:
            library_path: Path to the compiled parser library.
                         Defaults to 'build/siasun_robot.dylib'
        """
        self.library_path = library_path or self._find_library()
        self._init_parser()
        self._init_state()
        
    def _find_library(self) -> str:
        """Find the parser library"""
        script_dir = Path(__file__).parent
        candidates = [
            script_dir / "build" / "siasun_robot.dylib",
            script_dir / "build" / "siasun_robot.so",
            script_dir / "siasun_robot.dylib",
            script_dir / "siasun_robot.so",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        raise FileNotFoundError(
            f"Parser library not found. Build it first with build_parser.py"
        )
    
    def _init_parser(self):
        """Initialize the tree-sitter parser"""
        lib = ctypes.CDLL(self.library_path)
        tree_sitter_siasun_robot = lib.tree_sitter_siasun_robot
        tree_sitter_siasun_robot.restype = ctypes.c_void_p
        self.language = Language(tree_sitter_siasun_robot())
        self.parser = Parser(self.language)
        
    def _init_state(self):
        """Initialize interpreter state"""
        # Robot state
        self.robot = RobotState()
        
        # Variables
        self.integer_vars: dict[str, int] = {}      # I1, I2, ...
        self.real_vars: dict[str, float] = {}       # R1, R2, ...
        self.position_vars: dict[str, Position] = {}  # P1, P2, ...
        self.joint_vars: dict[str, JointAngles] = {}  # J1, J2, ...
        
        # I/O state
        self.digital_outputs: dict[int, bool] = {}   # OT#1, OT#2, ...
        self.digital_inputs: dict[int, bool] = {}    # IN#1, IN#2, ...
        self.general_outputs: dict[int, bool] = {}   # GO#1, GO#2, ...
        self.general_inputs: dict[int, bool] = {}    # GI#1, GI#2, ...
        
        # Program state
        self.source_code: bytes = b""
        self.tree = None
        self.labels: dict[str, Any] = {}  # Label -> node mapping
        self.current_node = None
        self.program_counter = 0
        self.call_stack: list[int] = []
        
        # Execution trace
        self.trace: list[str] = []
        self.verbose = True
        
    def load_file(self, filepath: str) -> None:
        """Load a Siasun program from file"""
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Program file not found: {filepath}")
        
        with open(path, 'rb') as f:
            self.source_code = f.read()
        
        self._parse()
        self._log(f"Loaded program: {filepath}")
        
    def load_source(self, source: str | bytes) -> None:
        """Load a Siasun program from string"""
        if isinstance(source, str):
            source = source.encode('utf-8')
        self.source_code = source
        self._parse()
        self._log("Loaded program from source")
        
    def _parse(self) -> None:
        """Parse the loaded source code"""
        self.tree = self.parser.parse(self.source_code)
        self._collect_labels()
        
    def _collect_labels(self) -> None:
        """Collect all label definitions from the program"""
        self.labels = {}
        
        def visit(node):
            if node.type == 'label_definition':
                label_text = self._get_text(node).strip()
                self.labels[label_text] = node
            for child in node.children:
                visit(child)
        
        if self.tree:
            visit(self.tree.root_node)
            
    def _get_text(self, node) -> str:
        """Get the source text for a node"""
        return self.source_code[node.start_byte:node.end_byte].decode('utf-8')
    
    def _log(self, message: str) -> None:
        """Log execution trace"""
        self.trace.append(message)
        if self.verbose:
            print(f"[SIASUN] {message}")
    
    def _resolve_value(self, value_str: str) -> float:
        """Resolve a value which can be a number or variable reference"""
        value_str = value_str.strip()
        
        # Try as literal number first
        try:
            return float(value_str)
        except ValueError:
            pass
        
        # Check if it's a variable reference
        if value_str.startswith('I') and value_str[1:].isdigit():
            return float(self.integer_vars.get(value_str, 0))
        elif value_str.startswith('R') and value_str[1:].isdigit():
            return self.real_vars.get(value_str, 0.0)
        elif value_str in self.integer_vars:
            return float(self.integer_vars[value_str])
        elif value_str in self.real_vars:
            return self.real_vars[value_str]
        
        # Default to 0
        return 0.0
    
    # ========================================================================
    # Execution Engine
    # ========================================================================
    
    def run(self) -> None:
        """Execute the loaded program"""
        if not self.tree:
            raise RuntimeError("No program loaded")
        
        self.robot.is_running = True
        self._log("=== Program Start ===")
        
        try:
            self._execute_statements(self.tree.root_node)
        except StopExecution:
            pass
        except Exception as e:
            self.robot.has_error = True
            self.robot.error_message = str(e)
            self._log(f"ERROR: {e}")
            raise
        finally:
            self.robot.is_running = False
            self._log("=== Program End ===")
            
    def _execute_statements(self, node) -> None:
        """Execute all statements in a node"""
        for child in node.children:
            self._execute_statement(child)
            
    def _execute_statement(self, node) -> None:
        """Execute a single statement"""
        node_type = node.type
        text = self._get_text(node).strip()
        
        # Skip comments and whitespace
        if node_type == 'comment' or not text:
            return
        
        # Dispatch based on statement type
        handlers = {
            'program_header': self._exec_nop,
            'program_footer': self._exec_end,
            'motion_statement': self._exec_motion,
            'io_statement': self._exec_io,
            'logic_statement': self._exec_logic,
            'control_statement': self._exec_control,
            'label_definition': self._exec_label,
            'macro_statement': self._exec_macro,
        }
        
        handler = handlers.get(node_type)
        if handler:
            handler(node)
        elif node_type in ('source_file',):
            # Container nodes - process children
            self._execute_statements(node)
        else:
            self._log(f"Unknown statement type: {node_type} -> {text[:50]}")
    
    # ========================================================================
    # Statement Handlers
    # ========================================================================
    
    def _exec_nop(self, node) -> None:
        """Execute NOP (program start)"""
        self._log("NOP - Program initialized")
        
    def _exec_end(self, node) -> None:
        """Execute END (program end)"""
        self._log("END - Program complete")
        raise StopExecution()
    
    def _exec_label(self, node) -> None:
        """Execute label definition (just a marker)"""
        label = self._get_text(node).strip()
        self._log(f"LABEL: {label}")
        
    def _exec_motion(self, node) -> None:
        """Execute motion statement (MOVJ, MOVL, etc.)"""
        text = self._get_text(node).strip()
        
        # Parse motion command
        parts = text.split()
        if not parts:
            return
            
        cmd = parts[0].upper()
        
        # Extract parameters
        params = self._parse_motion_params(text)
        
        if cmd == 'MOVJ':
            self._exec_movj(params)
        elif cmd == 'MOVL':
            self._exec_movl(params)
        elif cmd == 'MOVC':
            self._exec_movc(params)
        else:
            self._log(f"Motion: {text}")
            
    def _parse_motion_params(self, text: str) -> dict:
        """Parse motion command parameters"""
        params = {'raw': text}
        parts = text.split()
        
        if len(parts) > 1:
            params['target'] = parts[1]
        
        # Parse named parameters (VJ=50, ACC=100, etc.)
        for part in parts[2:]:
            if '=' in part:
                key, value = part.split('=', 1)
                params[key.upper()] = value
                
        return params
    
    def _exec_movj(self, params: dict) -> None:
        """Execute joint move"""
        target = params.get('target', 'P?')
        vj = params.get('VJ', '?')
        acc = params.get('ACC', '?')
        
        # Get target position
        if target in self.position_vars:
            target_pos = self.position_vars[target]
        else:
            target_pos = Position()  # Simulated
            
        # Check for OFFSET
        if 'OFFSET' in params:
            offset = self._parse_offset(params['OFFSET'])
            target_pos = target_pos + offset
            
        self._log(f"MOVJ -> {target} (VJ={vj}, ACC={acc})")
        self._log(f"  Target: {target_pos}")
        
        # Simulate movement
        self.robot.position = target_pos
        self.robot.velocity = float(vj) if vj != '?' else 0
        
    def _exec_movl(self, params: dict) -> None:
        """Execute linear move"""
        target = params.get('target', 'P?')
        vl = params.get('VL', '?')
        acc = params.get('ACC', '?')
        
        # Get target position
        if target in self.position_vars:
            target_pos = self.position_vars[target]
        else:
            target_pos = Position()  # Simulated
            
        # Check for OFFSET
        if 'OFFSET' in params:
            offset = self._parse_offset(params['OFFSET'])
            target_pos = target_pos + offset
            self._log(f"  Offset applied: {offset}")
            
        self._log(f"MOVL -> {target} (VL={vl}, ACC={acc})")
        self._log(f"  Target: {target_pos}")
        
        # Simulate movement
        self.robot.position = target_pos
        self.robot.velocity = float(vl) if vl != '?' else 0
        
    def _exec_movc(self, params: dict) -> None:
        """Execute circular move"""
        self._log(f"MOVC: {params.get('raw', '')}")
        
    def _parse_offset(self, offset_str: str) -> Position:
        """Parse OFFSET parameter (X,Y,Z,Rx,Ry,Rz)"""
        parts = offset_str.split(',')
        values = [self._resolve_value(v) for v in parts]
        while len(values) < 6:
            values.append(0.0)
        return Position(*values[:6])
    
    def _exec_io(self, node) -> None:
        """Execute I/O statement"""
        text = self._get_text(node).strip()
        
        # Parse OUT statement
        if text.startswith('OUT'):
            self._exec_out(text)
        elif text.startswith('IN'):
            self._exec_in(text)
        else:
            self._log(f"I/O: {text}")
            
    def _exec_out(self, text: str) -> None:
        """Execute OUT statement"""
        # Parse: OUT OT#1=ON or OUT GO#1=OFF
        import re
        match = re.search(r'(OT|GO)#(\d+)\s*=\s*(ON|OFF|\d+)', text, re.IGNORECASE)
        if match:
            io_type = match.group(1).upper()
            io_num = int(match.group(2))
            value = match.group(3).upper()
            
            if value == 'ON':
                state = True
            elif value == 'OFF':
                state = False
            else:
                state = int(value) != 0
                
            if io_type == 'OT':
                self.digital_outputs[io_num] = state
            else:
                self.general_outputs[io_num] = state
                
            self._log(f"OUT: {io_type}#{io_num} = {'ON' if state else 'OFF'}")
        else:
            self._log(f"OUT: {text}")
            
    def _exec_in(self, text: str) -> None:
        """Execute IN statement (read input)"""
        self._log(f"IN: {text}")
        
    def _exec_logic(self, node) -> None:
        """Execute logic statement (SET, ADD, IF, etc.)"""
        text = self._get_text(node).strip()
        parts = text.split()
        
        if not parts:
            return
            
        cmd = parts[0].upper()
        
        if cmd == 'SET':
            self._exec_set(parts)
        elif cmd == 'ADD':
            self._exec_add(parts)
        elif cmd == 'SUB':
            self._exec_sub(parts)
        elif cmd == 'MUL':
            self._exec_mul(parts)
        elif cmd == 'DIV':
            self._exec_div(parts)
        elif cmd == 'IF':
            self._exec_if(parts, node)
        else:
            self._log(f"Logic: {text}")
            
    def _exec_set(self, parts: list) -> None:
        """Execute SET statement"""
        if len(parts) < 3:
            return
            
        var_name = parts[1]
        value = parts[2]
        
        # Determine variable type and store
        if var_name.startswith('I'):
            self.integer_vars[var_name] = int(value)
            self._log(f"SET: {var_name} = {value} (integer)")
        elif var_name.startswith('R'):
            self.real_vars[var_name] = float(value)
            self._log(f"SET: {var_name} = {value} (real)")
        elif var_name.startswith('P'):
            if value == 'LPOS':
                self.position_vars[var_name] = Position(
                    self.robot.position.x,
                    self.robot.position.y,
                    self.robot.position.z,
                    self.robot.position.rx,
                    self.robot.position.ry,
                    self.robot.position.rz
                )
                self._log(f"SET: {var_name} = LPOS ({self.robot.position})")
            else:
                self.position_vars[var_name] = Position()
                self._log(f"SET: {var_name} = {value}")
        else:
            self._log(f"SET: {var_name} = {value}")
            
    def _exec_add(self, parts: list) -> None:
        """Execute ADD statement"""
        if len(parts) < 3:
            return
        var_name = parts[1]
        value = self._resolve_value(parts[2])
        
        if var_name.startswith('R'):
            if var_name in self.real_vars:
                self.real_vars[var_name] += value
            else:
                self.real_vars[var_name] = value
            self._log(f"ADD: {var_name} += {value} -> {self.real_vars[var_name]}")
        else:
            if var_name in self.integer_vars:
                self.integer_vars[var_name] += int(value)
            else:
                self.integer_vars[var_name] = int(value)
            self._log(f"ADD: {var_name} += {value} -> {self.integer_vars[var_name]}")
            
    def _exec_sub(self, parts: list) -> None:
        """Execute SUB statement"""
        if len(parts) < 3:
            return
        var_name = parts[1]
        value = self._resolve_value(parts[2])
        
        if var_name.startswith('R'):
            if var_name in self.real_vars:
                self.real_vars[var_name] -= value
                self._log(f"SUB: {var_name} -= {value} -> {self.real_vars[var_name]}")
        else:
            if var_name in self.integer_vars:
                self.integer_vars[var_name] -= int(value)
                self._log(f"SUB: {var_name} -= {value} -> {self.integer_vars[var_name]}")
            
    def _exec_mul(self, parts: list) -> None:
        """Execute MUL statement: MUL dest operand1 operand2"""
        if len(parts) < 4:
            # Two-operand form: MUL var value (var *= value)
            if len(parts) >= 3:
                var_name = parts[1]
                value = self._resolve_value(parts[2])
                if var_name.startswith('R'):
                    if var_name in self.real_vars:
                        self.real_vars[var_name] *= value
                        self._log(f"MUL: {var_name} *= {value} -> {self.real_vars[var_name]}")
                else:
                    if var_name in self.integer_vars:
                        self.integer_vars[var_name] *= int(value)
                        self._log(f"MUL: {var_name} *= {value} -> {self.integer_vars[var_name]}")
            return
        
        # Three-operand form: MUL dest op1 op2 (dest = op1 * op2)
        dest = parts[1]
        op1 = self._resolve_value(parts[2])
        op2 = self._resolve_value(parts[3])
        result = op1 * op2
        
        if dest.startswith('R'):
            self.real_vars[dest] = result
            self._log(f"MUL: {dest} = {op1} * {op2} -> {result}")
        else:
            self.integer_vars[dest] = int(result)
            self._log(f"MUL: {dest} = {op1} * {op2} -> {int(result)}")
            
    def _exec_div(self, parts: list) -> None:
        """Execute DIV statement: DIV dest operand1 operand2"""
        if len(parts) < 4:
            # Two-operand form: DIV var value (var /= value)
            if len(parts) >= 3:
                var_name = parts[1]
                value = self._resolve_value(parts[2])
                if value == 0:
                    self._log(f"DIV: Division by zero avoided")
                    return
                if var_name.startswith('R'):
                    if var_name in self.real_vars:
                        self.real_vars[var_name] /= value
                        self._log(f"DIV: {var_name} /= {value} -> {self.real_vars[var_name]}")
                else:
                    if var_name in self.integer_vars:
                        self.integer_vars[var_name] //= int(value)
                        self._log(f"DIV: {var_name} /= {value} -> {self.integer_vars[var_name]}")
            return
        
        # Three-operand form: DIV dest op1 op2 (dest = op1 / op2)
        dest = parts[1]
        op1 = self._resolve_value(parts[2])
        op2 = self._resolve_value(parts[3])
        
        if op2 == 0:
            self._log(f"DIV: Division by zero avoided")
            return
        
        result = op1 / op2
        
        if dest.startswith('R'):
            self.real_vars[dest] = result
            self._log(f"DIV: {dest} = {op1} / {op2} -> {result}")
        else:
            self.integer_vars[dest] = int(result)
            self._log(f"DIV: {dest} = {op1} / {op2} -> {int(result)}")
            
    def _exec_if(self, parts: list, node) -> None:
        """Execute IF statement"""
        # Simple parsing: IF var op value label/action
        if len(parts) < 5:
            self._log(f"IF: {' '.join(parts)}")
            return
            
        var_name = parts[1]
        op = parts[2]
        value = parts[3]
        action = parts[4]
        
        # Get variable value
        if var_name in self.integer_vars:
            var_val = self.integer_vars[var_name]
        elif var_name in self.real_vars:
            var_val = self.real_vars[var_name]
        else:
            var_val = 0
            
        # Compare
        cmp_val = float(value)
        result = False
        
        if op == '=':
            result = var_val == cmp_val
        elif op == '<':
            result = var_val < cmp_val
        elif op == '>':
            result = var_val > cmp_val
        elif op == '<=':
            result = var_val <= cmp_val
        elif op == '>=':
            result = var_val >= cmp_val
        elif op == '<>':
            result = var_val != cmp_val
            
        self._log(f"IF: {var_name}({var_val}) {op} {value} -> {result}")
        
        if result:
            if action == 'CALL':
                # Call subroutine
                if len(parts) > 5:
                    sub_name = parts[5]
                    self._log(f"  -> CALL {sub_name}")
            elif action.startswith('L'):
                # Jump to label
                self._log(f"  -> GOTO {action}")
                
    def _exec_control(self, node) -> None:
        """Execute control statement"""
        text = self._get_text(node).strip()
        self._log(f"Control: {text}")
        
    def _exec_macro(self, node) -> None:
        """Execute macro statement (TF, UF, etc.)"""
        text = self._get_text(node).strip()
        parts = text.split()
        
        if not parts:
            return
            
        cmd = parts[0].upper()
        
        if cmd == 'TF':
            # Set tool frame
            if len(parts) > 1:
                frame_num = parts[1].replace('#', '')
                try:
                    self.robot.tool_frame = int(frame_num)
                    self._log(f"TF: Tool Frame set to #{self.robot.tool_frame}")
                except ValueError:
                    self._log(f"TF: {text}")
        elif cmd == 'UF':
            # Set user frame
            if len(parts) > 1:
                frame_num = parts[1].replace('#', '')
                try:
                    self.robot.user_frame = int(frame_num)
                    self._log(f"UF: User Frame set to #{self.robot.user_frame}")
                except ValueError:
                    self._log(f"UF: {text}")
        else:
            self._log(f"Macro: {text}")
    
    # ========================================================================
    # Utility Methods
    # ========================================================================
    
    def get_state(self) -> dict:
        """Get current interpreter state as dictionary"""
        return {
            'robot': {
                'position': self.robot.position.__dict__,
                'joints': self.robot.joints.__dict__,
                'tool_frame': self.robot.tool_frame,
                'user_frame': self.robot.user_frame,
                'velocity': self.robot.velocity,
                'is_running': self.robot.is_running,
                'has_error': self.robot.has_error,
                'error_message': self.robot.error_message,
            },
            'variables': {
                'integers': dict(self.integer_vars),
                'reals': dict(self.real_vars),
                'positions': {k: v.__dict__ for k, v in self.position_vars.items()},
            },
            'io': {
                'digital_outputs': dict(self.digital_outputs),
                'digital_inputs': dict(self.digital_inputs),
            },
            'trace': list(self.trace),
        }
    
    def reset(self) -> None:
        """Reset interpreter state"""
        self._init_state()
        self._log("Interpreter reset")
        
    def print_ast(self) -> None:
        """Print the Abstract Syntax Tree"""
        if not self.tree:
            print("No program loaded")
            return
            
        def walk(node, level=0):
            indent = "  " * level
            text = self._get_text(node).strip().replace('\n', '\\n')
            if len(text) > 50:
                text = text[:50] + "..."
            print(f"{indent}[{node.type}] {text}")
            for child in node.children:
                walk(child, level + 1)
                
        walk(self.tree.root_node)


class StopExecution(Exception):
    """Exception to signal normal program termination"""
    pass


# ============================================================================
# Main entry point for testing
# ============================================================================

def main():
    """Test the interpreter with sample programs"""
    import sys
    
    interpreter = SiasunInterpreter()
    
    if len(sys.argv) > 1:
        # Load file from command line
        filepath = sys.argv[1]
        interpreter.load_file(filepath)
    else:
        # Use built-in test program
        test_program = """
NOP

// Set tool frame
TF #1

// Initialize variables
SET I1 0
SET R1 10.5
SET P1 LPOS

// Motion test
MOVL P1 VL=500 ACC=50 CNT=0
MOVL P1 VL=500 ACC=50 CNT=0 OFFSET=100,0,0,0,0,0

// I/O test
OUT OT#1=ON

// Logic test
ADD I1 1
IF I1 < 5 L10

END
"""
        interpreter.load_source(test_program)
    
    print("\n=== AST ===")
    interpreter.print_ast()
    
    print("\n=== Execution ===")
    interpreter.run()
    
    print("\n=== Final State ===")
    state = interpreter.get_state()
    print(f"Position: {interpreter.robot.position}")
    print(f"Tool Frame: {interpreter.robot.tool_frame}")
    print(f"Variables: I={interpreter.integer_vars}, R={interpreter.real_vars}")
    print(f"Outputs: {interpreter.digital_outputs}")


if __name__ == "__main__":
    main()
