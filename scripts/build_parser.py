"""
Build tree-sitter parser for Siasun Robot Language.

Prerequisites:
    - tree-sitter Python package: pip install tree-sitter
    - tree-sitter-cli (for generating parser): npm install -g tree-sitter-cli
      OR: pip install tree-sitter-cli
    - C compiler (gcc/clang)

Usage:
    python build_parser.py
"""

import os
import sys
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
GRAMMAR_DIR = SCRIPT_DIR / "tree-sitter-siasun"
OUTPUT_DIR = SCRIPT_DIR / "build"
OUTPUT_FILE = OUTPUT_DIR / "siasun_robot.so"


def compile_parser():
    """Compile the parser C code into a shared library."""
    import platform
    
    print("Compiling parser into shared library...")
    
    # Create output directory
    OUTPUT_DIR.mkdir(exist_ok=True)
    
    # Source files
    parser_c = GRAMMAR_DIR / "src" / "parser.c"
    if not parser_c.exists():
        raise RuntimeError(f"Parser source not found: {parser_c}")
    
    # Determine compiler and flags
    system = platform.system()
    if system == "Darwin":  # macOS
        compiler = "clang"
        output_flag = "-dynamiclib"
        ext = "dylib"
    elif system == "Windows":
        compiler = "cl"
        output_flag = "/LD"
        ext = "dll"
    else:  # Linux
        compiler = "gcc"
        output_flag = "-shared"
        ext = "so"
    
    output_file = OUTPUT_DIR / f"siasun_robot.{ext}"
    
    # Compile command
    compile_cmd = [
        compiler,
        output_flag,
        "-fPIC",
        "-O2",
        "-std=c99",
        "-I", str(GRAMMAR_DIR / "src"),
        str(parser_c),
        "-o", str(output_file)
    ]
    
    print(f"Running: {' '.join(compile_cmd)}")
    result = subprocess.run(compile_cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Compilation error:\n{result.stderr}")
        raise RuntimeError("Failed to compile parser")
    
    print(f"Successfully compiled: {output_file}")
    return output_file


def main():
    os.chdir(SCRIPT_DIR)

    # Step 1: Generate parser C code from grammar.js
    print("Generating parser from grammar.js...")
    result = subprocess.run(
        ["tree-sitter", "generate"],
        cwd=GRAMMAR_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error generating parser:\n{result.stderr}")
        print(f"Output:\n{result.stdout}")
        raise RuntimeError("tree-sitter generate failed")
    print("Parser source files generated successfully.")

    # Step 2: Compile the shared library
    try:
        lib_file = compile_parser()
        print(f"\n✓ Build successful!")
        print(f"  Library: {lib_file}")
        print(f"\nTo use in Python:")
        print(f"  from tree_sitter import Language")
        print(f"  # Note: Modern tree-sitter API may require additional setup")
    except Exception as e:
        print(f"Compilation failed: {e}")
        raise


if __name__ == "__main__":
    main()
