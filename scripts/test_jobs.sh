#!/bin/bash
# Test all job files from rc5/jobs folder
# These files have #JOB/#INFO/#INST/#PARAM structure

echo "Testing RC5 Job Files"
echo "===================="
echo

for f in rc5/jobs/*.siasun; do
    echo "=== $(basename $f) ==="
    
    # Extract instructions from job file
    python extract_instructions.py "$f" > /tmp/test_job.siasun 2>&1
    
    if [ $? -ne 0 ]; then
        echo "❌ FAILED to extract instructions"
        continue
    fi
    
    # Run interpreter
    python siasun_interpreter.py /tmp/test_job.siasun 2>&1 | \
        grep -E "(Traceback|Exception|Parse.*ERROR|Final State|Position:|Tool Frame:|Variables:|Outputs:)" | \
        grep -v "ERROR.*=" | \
        head -8
    
    # Check if successful
    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        echo "✅ PASS"
    else
        echo "❌ FAIL"
    fi
    echo
done

echo "===================="
echo "All job tests completed"
