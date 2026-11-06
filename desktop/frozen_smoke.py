"""Verify worker dispatch and bundled imports without paid/external model calls."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as directory:
    runtime = subprocess.run([sys.argv[1], '--desktop-worker', '--check-runtime'],
                             capture_output=True, text=True, timeout=90)
    assert runtime.returncode == 0, runtime.stderr
    assert 'runtime-ready' in runtime.stdout, (runtime.stdout, runtime.stderr)
    request = Path(directory) / 'request.json'
    request.write_text(json.dumps({'ticker': '../../invalid'}), encoding='utf-8')
    result = subprocess.run([sys.argv[1], '--desktop-worker', str(request)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert 'TA_DESKTOP_EVENT:' in result.stdout, result.stderr
    assert 'ValueError' in result.stdout, result.stdout
    assert not (request.parent / 'complete_report.md').exists()
    print('Frozen worker dispatch OK')
