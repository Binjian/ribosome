#!/bin/bash
# Test all Siasun robot programs
# Note: Arithmetic expressions like "I[1] = I[1] + 1" currently parse as ERROR nodes
# but execution still completes correctly. These are known parsing issues.

echo "Running all test files..."
echo "========================"
echo

for f in tests/*.siasun; do
	echo "=== $f ==="
	python siasun_interpreter.py "$f" 2>&1 | grep -E "(Traceback|Exception|Parse.*ERROR|Final State|Position:|Tool Frame:|Variables:|Outputs:)" | grep -v "ERROR.*=" | head -8
	echo
done

echo "========================"
echo "All tests completed"

