for f in tests/*.siasun; do
	echo "=== $f ==="
	python siasun_interpreter.py "$f" 2>&1 | grep -E "(ERROR|Traceback|Final State|Position:|Tool Frame:|Variables:|Outputs:)" | head -8
done
