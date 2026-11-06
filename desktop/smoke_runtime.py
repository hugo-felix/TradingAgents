"""Bounded teardown for opt-in native GUI verification, never production startup."""
import json
import os
import sys
import threading


def exit_smoke(result: dict, cleanup=None):
    """Do not let a GUI backend's remaining threads keep a test application alive."""
    if cleanup:
        thread = threading.Thread(target=cleanup, daemon=True)
        thread.start()
        thread.join(timeout=2)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    sys.stderr.flush()
    # sys.exit waits for non-daemon Cocoa/WebView threads; the test process has
    # no user data and all app cleanup has already run with a bounded timeout.
    os._exit(0 if result.get('ok') else 1)
