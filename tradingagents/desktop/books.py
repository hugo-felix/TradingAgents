"""Local book identities and immutable originals; registration performs no parsing or AI calls."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

from tradingagents.desktop.store import now

METADATA_FIELDS = ('title', 'author', 'translator', 'language', 'edition', 'source', 'usage_scope')


def validate_metadata(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(METADATA_FIELDS):
        raise ValueError('请完整填写书名、作者、译者、语言、版次、来源和使用范围。')
    if any(not isinstance(value[k], str) or not value[k].strip() for k in METADATA_FIELDS):
        raise ValueError('书籍信息不能为空；无译者请明确填写“无”。')
    return {k: value[k].strip() for k in METADATA_FIELDS}


class BookRegistry:
    def __init__(self, store):
        self.store = store
        self.originals = store.directory / 'books' / 'originals'
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS books (
                book_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
                document TEXT NOT NULL)''')

    def register(self, path: Path, metadata: dict) -> dict:
        metadata = validate_metadata(metadata)
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in {'.pdf', '.epub'} or not path.is_file():
            raise ValueError('请选择本地 PDF 或 EPUB 书籍文件。')
        self.originals.mkdir(parents=True, exist_ok=True)
        # Hash the bytes actually copied, so later source changes cannot alter this identity.
        temporary = None
        try:
            digest = hashlib.sha256()
            size = 0
            with tempfile.NamedTemporaryFile(dir=self.originals, delete=False) as target:
                temporary = Path(target.name)
                with path.open('rb') as source:
                    while block := source.read(1024 * 1024):
                        target.write(block)
                        digest.update(block)
                        size += len(block)
            self._validate_format(temporary, suffix)
            fingerprint = digest.hexdigest()
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute('SELECT document FROM books WHERE sha256=?',
                                      (fingerprint,)).fetchone()
                if existing:
                    document = json.loads(existing[0])
                    if document['metadata'] != metadata:
                        raise ValueError('相同文件已有不同登记信息，请核对版次、译本与来源。')
                    return {'duplicate': True, 'book': document}
                document = {
                    'book_id': uuid4().hex, 'sha256': fingerprint, 'size_bytes': size,
                    'format': suffix[1:], 'original_name': path.name, 'registered_at': now(),
                    'metadata': metadata, 'parsing': 'not_started', 'indexing': 'not_started',
                }
                # The destination is derived from our digest, never caller-supplied metadata.
                os.replace(temporary, self.originals / fingerprint)
                db.execute('INSERT INTO books VALUES (?, ?, ?)', (
                    document['book_id'], fingerprint, json.dumps(document, ensure_ascii=False)))
            return {'duplicate': False, 'book': document}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_format(path: Path, suffix: str) -> None:
        if suffix == '.pdf':
            with path.open('rb') as source:
                valid = source.read(5) == b'%PDF-'
        else:
            try:
                with zipfile.ZipFile(path) as archive:
                    info = archive.getinfo('mimetype')
                    valid = (info.file_size == len(b'application/epub+zip')
                             and archive.read(info) == b'application/epub+zip')
            except (zipfile.BadZipFile, KeyError, RuntimeError, NotImplementedError):
                valid = False
        if not valid:
            raise ValueError('文件头与所选书籍格式不符；未登记，也未执行解析。')

    def list_books(self) -> list[dict]:
        with self.store.connect() as db:
            rows = db.execute('SELECT document FROM books ORDER BY rowid DESC').fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, book_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM books WHERE book_id=?', (book_id,)).fetchone()
        if row is None:
            raise ValueError('未找到书籍登记。')
        return json.loads(row[0])

    def verify(self, book_id: str) -> dict:
        document = self.get(book_id)
        path = self.originals / document['sha256']
        try:
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
        except FileNotFoundError:
            status = 'missing'
        except OSError:
            status = 'unreadable'
        else:
            status = 'available' if digest == document['sha256'] else 'changed'
        return {'book_id': book_id, 'original_status': status, 'checked_at': now()}
