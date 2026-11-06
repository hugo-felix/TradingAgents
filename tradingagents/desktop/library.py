"""Discover known local report directories and update snapshots in the background."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from tradingagents.desktop.store import DesktopStore, now


def discover_report_roots(engine_root: Path, *, cwd=None, executable=None, source_file=None) -> list[Path]:
    """Use explicit engine/project locations, including a Finder-launched app in dist/."""
    roots = [Path(engine_root).expanduser().resolve()]
    anchors = [Path(cwd or Path.cwd()), Path(executable or sys.executable).parent,
               Path(source_file or __file__).parent]
    for anchor in anchors:
        for directory in (anchor, *anchor.parents):
            if (directory / 'pyproject.toml').is_file() and (directory / 'tradingagents').is_dir():
                roots.extend((directory / 'artifacts', directory / 'reports'))
                break
    return list(dict.fromkeys(path.resolve() for path in roots))


class ReportLibrary:
    def __init__(self, store: DesktopStore, roots, *, interval=15, settle_seconds=1):
        self._store = store
        self._interval = interval
        self._settle_seconds = settle_seconds
        self._lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        saved = store.setting('report_directories') or []
        self._roots = list(dict.fromkeys(Path(path).expanduser().resolve() for path in [*roots, *saved]))
        store.setting('report_directories', [str(path) for path in self._roots])
        self._state = {'status': 'starting', 'revision': 0, 'checked_at': None, 'errors': []}

    def status(self):
        with self._lock:
            return {**self._state, 'directories': [str(path) for path in self._roots]}

    def add_directory(self, directory: Path):
        directory = Path(directory).expanduser().resolve()
        with self._lock:
            if directory not in self._roots:
                self._roots.append(directory)
                self._store.setting('report_directories', [str(path) for path in self._roots])
        self._wake.set()

    def scan_once(self):
        with self._scan_lock:
            with self._lock:
                self._state['status'] = 'scanning'
                roots = tuple(self._roots)
            result = {'added': 0, 'skipped': 0, 'errors': []}
            for directory in roots:
                if self._stop.is_set():
                    break
                if not directory.is_dir():
                    continue
                try:
                    found = self._store.import_directory(
                        directory, only_changed=True, exclude=(self._store.directory / 'runs',),
                        settle_seconds=self._settle_seconds,
                    )
                    result['added'] += found['added']
                    result['skipped'] += found['skipped']
                    result['errors'].extend(found['errors'])
                except OSError:
                    result['errors'].append({'file': directory.name, 'error': '目录暂时不可读'})
            with self._lock:
                self._state.update(status='watching', checked_at=now(), errors=result['errors'][:20])
                self._state['revision'] += result['added']
            return result

    def _run(self):
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                with self._lock:
                    self._state.update(status='watching', errors=[{'error': '报告目录检查未完成，下次自动重试'}])
            self._wake.wait(self._interval)
            self._wake.clear()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name='stockseek-reports')
        self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
