"""Content-addressed JSON evidence, independent of replaceable vendor caches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from .files import locked


class SnapshotIntegrityError(ValueError):
    """An archived object is missing, unreadable, or does not match its address."""


def canonical_json(payload: dict) -> bytes:
    """The byte contract used by the evidence fingerprint; not HTTP response bytes."""
    if not isinstance(payload, dict):
        raise ValueError('A JSON snapshot must be an object')
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def _snapshot_path(directory: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Invalid snapshot SHA-256')
    return Path(directory) / f'{digest}.json'


def load_json_snapshot(directory: Path, digest: str) -> dict:
    """Read exactly this object, checking its bytes before decoding; never fetch."""
    path = _snapshot_path(directory, digest)
    try:
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise SnapshotIntegrityError('Snapshot fingerprint mismatch')
        payload = json.loads(content)
        if not isinstance(payload, dict) or canonical_json(payload) != content:
            raise SnapshotIntegrityError('Snapshot is not a canonical JSON object')
        return payload
    except (OSError, ValueError) as exc:
        if isinstance(exc, SnapshotIntegrityError):
            raise
        raise SnapshotIntegrityError('Snapshot missing or unreadable') from exc


def save_json_snapshot(directory: Path, payload: dict) -> str:
    """Publish once under a lock. Existing corrupt evidence is never silently repaired.

    Write failures propagate: callers cannot claim durable evidence on a full or
    read-only disk. Temp files are removed even if publication fails.
    """
    content = canonical_json(payload)
    digest = hashlib.sha256(content).hexdigest()
    path = _snapshot_path(directory, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        if path.exists():
            load_json_snapshot(directory, digest)
            return digest
        temp = path.with_name(f'{path.name}.{uuid.uuid4().hex}.tmp')
        try:
            temp.write_bytes(content)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return digest
