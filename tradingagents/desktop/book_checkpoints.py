"""Private extraction checkpoints; never exposed as published book text."""

import hashlib
import json


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class BookCheckpoint:
    def __init__(self, store, job_id):
        self.store, self.job_id = store, job_id
        self.binding = None
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS book_checkpoint_headers (
                job_id TEXT PRIMARY KEY, binding TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS book_checkpoint_units (
                job_id TEXT NOT NULL, ordinal INTEGER NOT NULL, document TEXT NOT NULL,
                digest TEXT NOT NULL, PRIMARY KEY (job_id, ordinal))''')

    def load(self, book_id, sha256, parser):
        self.binding = _encode([book_id, sha256, parser])
        with self.store.connect() as db:
            row = db.execute('SELECT binding FROM book_checkpoint_headers WHERE job_id=?',
                             (self.job_id,)).fetchone()
            if row is None:
                if self.count(db):
                    raise ValueError('Checkpoint header is missing')
                db.execute('INSERT INTO book_checkpoint_headers VALUES (?, ?)',
                           (self.job_id, self.binding))
            elif row[0] != self.binding:
                raise ValueError('Checkpoint source or parser changed')
            rows = db.execute('SELECT ordinal, document, digest FROM book_checkpoint_units '
                              'WHERE job_id=? ORDER BY ordinal', (self.job_id,)).fetchall()
        units = []
        for ordinal, document, digest in rows:
            unit = json.loads(document)
            if (ordinal != len(units) + 1 or unit.get('ordinal') != ordinal
                    or self._digest(document) != digest):
                raise ValueError('Invalid extraction checkpoint')
            units.append(unit)
        return units

    def _digest(self, document):
        return hashlib.sha256((self.binding + '\n' + document).encode('utf-8')).hexdigest()

    def append(self, unit):
        document = _encode(unit)
        with self.store.connect() as db:
            db.execute('INSERT INTO book_checkpoint_units VALUES (?, ?, ?, ?)',
                       (self.job_id, unit['ordinal'], document, self._digest(document)))

    def count(self, db):
        return db.execute('SELECT COUNT(*) FROM book_checkpoint_units WHERE job_id=?',
                          (self.job_id,)).fetchone()[0]

    def clone_to(self, target_id, db):
        db.execute('INSERT INTO book_checkpoint_headers SELECT ?, binding '
                   'FROM book_checkpoint_headers WHERE job_id=?', (target_id, self.job_id))
        db.execute('INSERT INTO book_checkpoint_units SELECT ?, ordinal, document, digest '
                   'FROM book_checkpoint_units WHERE job_id=?', (target_id, self.job_id))

    def delete(self, db):
        db.execute('DELETE FROM book_checkpoint_units WHERE job_id=?', (self.job_id,))
        db.execute('DELETE FROM book_checkpoint_headers WHERE job_id=?', (self.job_id,))
