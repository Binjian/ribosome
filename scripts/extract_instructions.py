#!/usr/bin/env python3
"""
Extract instructions from Siasun job files.
Job files have format:
#JOB
...metadata...
#INST
...robot instructions...
#PARAM
"""

import sys
import os

def extract_instructions(job_file_path):
    """Extract instructions between #INST and #PARAM"""
    with open(job_file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # Find #INST and #PARAM markers
    inst_start = None
    inst_end = None
    
    for i, line in enumerate(lines):
        if line.strip() == '#INST':
            inst_start = i + 1
        elif line.strip() == '#PARAM':
            inst_end = i
            break
    
    if inst_start is None:
        print(f"Warning: No #INST marker found in {job_file_path}", file=sys.stderr)
        return None
    
    if inst_end is None:
        # No #PARAM found, take rest of file
        inst_end = len(lines)
    
    # Extract instruction lines
    instructions = lines[inst_start:inst_end]
    
    # Join and return
    return ''.join(instructions)

def main():
    if len(sys.argv) < 2:
        print("Usage: extract_instructions.py <job_file>", file=sys.stderr)
        sys.exit(1)
    
    job_file = sys.argv[1]
    
    if not os.path.exists(job_file):
        print(f"Error: File not found: {job_file}", file=sys.stderr)
        sys.exit(1)
    
    instructions = extract_instructions(job_file)
    
    if instructions is None:
        sys.exit(1)
    
    print(instructions, end='')

if __name__ == '__main__':
    main()
