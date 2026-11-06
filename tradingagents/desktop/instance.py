"""OS-owned workspace lock, automatically released after a crash."""

import os
from pathlib import Path


class WorkspaceLock:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.file = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = (self.directory / 'workspace.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0, 2)
                if self.file.tell() == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise ValueError('此工作空间已打开，请使用已有窗口。') from exc
        return self

    def __exit__(self, *_):
        if self.file:
            # Closing the descriptor releases both flock and Windows byte locks.
            self.file.close()
