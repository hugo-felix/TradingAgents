"""Archived vendor evidence must survive cache replacement and detect damage."""

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.dataflows import snapshots


def test_canonical_identity_is_order_independent_and_preserves_unicode(tmp_path):
    digest = snapshots.save_json_snapshot(tmp_path, {'名称': '股票', 'value': 10})
    assert snapshots.save_json_snapshot(tmp_path, {'value': 10, '名称': '股票'}) == digest
    content = (tmp_path / f'{digest}.json').read_bytes()
    assert hashlib.sha256(content).hexdigest() == digest
    assert '股票'.encode() in content
    assert snapshots.load_json_snapshot(tmp_path, digest) == {'名称': '股票', 'value': 10}


def test_concurrent_writers_publish_one_object_without_temp_files(tmp_path):
    with ThreadPoolExecutor(max_workers=6) as pool:
        digests = list(pool.map(lambda _: snapshots.save_json_snapshot(tmp_path, {'v': 1}), range(12)))
    assert len(set(digests)) == 1
    assert len(list(tmp_path.glob('*.json'))) == 1
    assert not list(tmp_path.glob('*.tmp'))
    assert snapshots.load_json_snapshot(tmp_path, digests[0]) == {'v': 1}


def test_existing_corrupt_evidence_is_not_replaced(tmp_path):
    digest = snapshots.save_json_snapshot(tmp_path, {'v': 1})
    path = tmp_path / f'{digest}.json'
    path.write_bytes(b'corrupt')
    with pytest.raises(snapshots.SnapshotIntegrityError, match='fingerprint'):
        snapshots.load_json_snapshot(tmp_path, digest)
    with pytest.raises(snapshots.SnapshotIntegrityError, match='fingerprint'):
        snapshots.save_json_snapshot(tmp_path, {'v': 1})
    assert path.read_bytes() == b'corrupt'


def test_missing_snapshot_is_explicit(tmp_path):
    with pytest.raises(snapshots.SnapshotIntegrityError, match='missing'):
        snapshots.load_json_snapshot(tmp_path, 'a' * 64)


@pytest.mark.parametrize('digest', ['../outside', 'A' * 64, '', None])
def test_snapshot_address_cannot_be_a_path(tmp_path, digest):
    with pytest.raises(ValueError, match='SHA-256'):
        snapshots.load_json_snapshot(tmp_path, digest)


@pytest.mark.parametrize('payload', [{'v': float('nan')}, {'v': float('inf')}, []])
def test_invalid_payload_is_never_published(tmp_path, payload):
    with pytest.raises(ValueError):
        snapshots.save_json_snapshot(tmp_path, payload)
    assert not list(tmp_path.iterdir())


def test_failed_publish_cleans_temp_and_does_not_claim_success(tmp_path, monkeypatch):
    def fail(*args):
        raise PermissionError('locked')

    monkeypatch.setattr(snapshots.os, 'replace', fail)
    with pytest.raises(PermissionError):
        snapshots.save_json_snapshot(tmp_path, {'v': 1})
    assert not list(tmp_path.glob('*.json'))
    assert not list(tmp_path.glob('*.tmp'))


def test_valid_hash_with_noncanonical_bytes_is_rejected(tmp_path):
    content = b'{"v": 1}'
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / f'{digest}.json').write_bytes(content)
    with pytest.raises(snapshots.SnapshotIntegrityError, match='canonical'):
        snapshots.load_json_snapshot(tmp_path, digest)
