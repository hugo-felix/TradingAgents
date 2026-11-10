"""Separate, local-only book extraction with durable lifecycle and atomic publication."""

from __future__ import annotations

import copy
import json
import threading
from uuid import uuid4

from tradingagents.desktop.book_text import BookTextLibrary
from tradingagents.desktop.store import now

ACTIVE = {'running', 'cancelling'}


class BookParseCancelled(ValueError):
    pass


class BookTextJobs:
    def __init__(self, store):
        self.store = store
        self.library = BookTextLibrary(store)
        self.lock = threading.RLock()
        self.thread = None
        self.current = None
        self.closed = False
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS book_text_jobs (
                id TEXT PRIMARY KEY, book_id TEXT NOT NULL, document TEXT NOT NULL)''')
            for row in db.execute('SELECT document FROM book_text_jobs').fetchall():
                job = json.loads(row[0])
                if job['status'] in ACTIVE:
                    job.update(status='interrupted', finished_at=now())
                    self._save(job, db)

    @staticmethod
    def _save(job, db):
        db.execute('INSERT INTO book_text_jobs VALUES (?, ?, ?) '
                   'ON CONFLICT(id) DO UPDATE SET document=excluded.document',
                   (job['id'], job['book_id'], json.dumps(job, ensure_ascii=False)))

    def status(self, book_id):
        self.library.books.get(book_id)
        with self.lock, self.store.connect() as db:
            row = db.execute('SELECT document FROM book_text_jobs WHERE book_id=? '
                             'ORDER BY rowid DESC LIMIT 1', (book_id,)).fetchone()
        return json.loads(row[0]) if row else {'status': 'idle', 'book_id': book_id}

    def start(self, book_id):
        self.library.books.get(book_id)
        with self.lock:
            if self.closed:
                raise ValueError('应用正在退出，不能开始解析。')
            if self.current and self.current['status'] in ACTIVE:
                if self.current['book_id'] == book_id:
                    return copy.deepcopy(self.current)
                raise ValueError('已有书籍正在解析，请等待完成或先取消。')
            job = {'id': uuid4().hex, 'book_id': book_id, 'status': 'running',
                   'started_at': now(), 'completed_units': 0, 'parse_id': None,
                   'error': None, 'finished_at': None}
            with self.store.connect() as db:
                self._save(job, db)
            self.current = job
            self.thread = threading.Thread(target=self._run, args=(job,), daemon=True)
            try:
                self.thread.start()
            except Exception:
                job.update(status='failed', error='无法启动本地解析任务。', finished_at=now())
                with self.store.connect() as db:
                    self._save(job, db)
            return copy.deepcopy(job)

    def cancel(self, job_id):
        with self.lock, self.store.connect() as db:
            row = db.execute('SELECT document FROM book_text_jobs WHERE id=?',
                             (job_id,)).fetchone()
            if row is None:
                raise ValueError('未找到该解析任务。')
            job = json.loads(row[0])
            if job['status'] in ACTIVE:
                # Exact job identity prevents a stale button cancelling a retry.
                if self.current is None or self.current['id'] != job_id:
                    raise ValueError('该解析任务不属于当前应用。')
                self.current['status'] = 'cancelling'
                job = self.current
                self._save(job, db)
            return copy.deepcopy(job)

    def _progress(self, job, completed):
        with self.lock:
            if job['status'] == 'cancelling' or self.closed:
                raise BookParseCancelled('解析已取消。')
            job['completed_units'] = completed
            with self.store.connect() as db:
                self._save(job, db)

    def _run(self, job):
        try:
            document = self.library.parse(
                job['book_id'], persist=False,
                progress=lambda completed: self._progress(job, completed))
            with self.lock:
                if job['status'] == 'cancelling' or self.closed:
                    raise BookParseCancelled('解析已取消。')
                # Both rows commit together. Cancel either wins before publication,
                # or observes an already completed, immutable version.
                completed = dict(job, status='completed', parse_id=document['parse_id'],
                                 finished_at=now())
                with self.store.connect() as db:
                    self.library.publish(document, db)
                    self._save(completed, db)
                job.update(completed)
        except Exception as error:
            with self.lock:
                cancelled = isinstance(error, BookParseCancelled) or job['status'] == 'cancelling'
                job.update(status='cancelled' if cancelled else 'failed',
                           error=None if cancelled else '本地解析失败；请核验文件、依赖和大小限制后重试。',
                           finished_at=now(), parse_id=None)
                with self.store.connect() as db:
                    self._save(job, db)

    def close(self):
        with self.lock:
            self.closed = True
            if self.current and self.current['status'] in ACTIVE:
                self.cancel(self.current['id'])
