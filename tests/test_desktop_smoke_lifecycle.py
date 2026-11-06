"""UI verification must never leave an application on the user's desktop."""
import subprocess
import sys


def test_native_smoke_exits_even_with_backend_threads():
    script = '''
import threading, time
from desktop.smoke_runtime import exit_smoke
threading.Thread(target=lambda: time.sleep(60)).start()
exit_smoke({'ok': True}, lambda: None)
'''
    process = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert '"ok": true' in stdout
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_native_smoke_requires_explicit_opt_in():
    process = subprocess.run([sys.executable, 'desktop/ui_smoke.py'], capture_output=True, text=True, timeout=5)
    assert process.returncode == 2
    assert '--native' in process.stderr
