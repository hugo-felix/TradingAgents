"""Verify worker dispatch and bundled imports without paid/external model calls."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

executable = Path(sys.argv[1]).resolve()
# A windowed Windows executable has no stdout; use the shipped console worker.
if sys.platform == 'win32' and executable.name.lower() == 'stockseek.exe':
    executable = executable.with_name('StockSeekWorker.exe')

with tempfile.TemporaryDirectory() as directory:
    runtime = subprocess.run([str(executable), '--desktop-worker', '--check-runtime'],
                             capture_output=True, text=True, encoding='utf-8', errors='replace',
                             cwd=directory, timeout=90)
    assert runtime.returncode == 0, runtime.stderr
    assert 'runtime-ready' in runtime.stdout, (runtime.stdout, runtime.stderr)
    request = Path(directory) / 'request.json'
    request.write_text(json.dumps({'ticker': '../../invalid'}), encoding='utf-8')
    result = subprocess.run([str(executable), '--desktop-worker', str(request)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            cwd=directory, timeout=60)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert 'TA_DESKTOP_EVENT:' in result.stdout, result.stderr
    assert 'ValueError' in result.stdout, result.stdout
    assert not (request.parent / 'complete_report.md').exists()
    dividend = subprocess.run([str(executable), '--dividend-worker', '--check-runtime'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              cwd=directory, timeout=90)
    assert dividend.returncode == 0, (dividend.stdout, dividend.stderr)
    assert 'dividend-runtime-ready' in dividend.stdout, dividend.stdout
    assert not (Path(directory) / 'data').exists(), 'Runtime check must not fetch market data'
    print('Frozen research and dividend worker dispatch OK')
