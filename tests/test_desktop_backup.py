import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest

from tradingagents.desktop import backup
from tradingagents.desktop.books import METADATA_FIELDS, BookRegistry
from tradingagents.desktop.instance import WorkspaceLock
from tradingagents.desktop.store import DesktopStore


def workspace(tmp_path):
    store = DesktopStore(tmp_path / 'workspace')
    with store.connect() as db:
        db.execute('INSERT INTO holdings VALUES (?, ?, ?)', ('AAPL', 3, 123.5))
    book = tmp_path / 'input.pdf'
    book.write_bytes(b'%PDF-1.4\nsynthetic book')
    registered = BookRegistry(store).register(book, dict.fromkeys(METADATA_FIELDS, '合成资料'))['book']
    artifact = store.directory / 'runs' / 'test' / 'summary.txt'
    artifact.parent.mkdir(parents=True)
    artifact.write_text('合成研究结果', encoding='utf-8')
    return store, registered


def rewrite(archive, change):
    with zipfile.ZipFile(archive) as bundle:
        files = {info.filename: bundle.read(info) for info in bundle.infolist()}
    manifest = json.loads(files['manifest.json'])
    change(files, manifest)
    files['manifest.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(archive, 'w') as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)


def test_backup_verify_restore_preserves_database_books_and_artifacts(tmp_path):
    store, book = workspace(tmp_path)
    archive = tmp_path / 'backup.zip'
    manifest = backup.create_backup(store.directory, archive)
    assert backup.verify_backup(archive) == manifest
    destination = tmp_path / 'restored'
    assert backup.restore_backup(archive, destination) == manifest
    assert not (destination / backup.INCOMPLETE).exists()
    restored = DesktopStore(destination)
    with restored.connect() as db:
        assert tuple(db.execute('SELECT * FROM holdings').fetchone()) == ('AAPL', 3, 123.5)
    assert BookRegistry(restored).get(book['book_id']) == book
    assert BookRegistry(restored).verify(book['book_id'])['original_status'] == 'available'
    assert (destination / 'runs/test/summary.txt').read_text(encoding='utf-8') == '合成研究结果'


def test_backup_excludes_browser_state_env_and_runtime_files(tmp_path):
    store, _ = workspace(tmp_path)
    root = store.directory
    for name in ('.env', '.ENV.enterprise', 'nested/.env.example', 'webview/state'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic private setting', encoding='utf-8')
    archive = tmp_path / 'backup.zip'
    manifest = backup.create_backup(root, archive)
    names = {row['path'] for row in manifest['files']}
    assert 'workspace.sqlite3' in names
    assert not names.intersection({'.env', '.ENV.enterprise', 'nested/.env.example', 'webview/state',
                                   'workspace.lock', 'workspace.sqlite3-wal', 'workspace.sqlite3-shm'})


def test_running_workspace_and_existing_destination_are_rejected(tmp_path):
    store, _ = workspace(tmp_path)
    archive = tmp_path / 'backup.zip'
    with WorkspaceLock(store.directory), pytest.raises(ValueError, match='已打开'):
        backup.create_backup(store.directory, archive)
    assert not archive.exists()
    backup.create_backup(store.directory, archive)
    original = archive.read_bytes()
    with pytest.raises(ValueError, match='不会覆盖'):
        backup.create_backup(store.directory, archive)
    assert archive.read_bytes() == original
    with pytest.raises(ValueError, match='之外'):
        backup.create_backup(store.directory, store.directory / 'backup.zip')
    with pytest.raises(ValueError, match='新目录'):
        backup.restore_backup(archive, store.directory)
    assert BookRegistry(store).list_books()


@pytest.mark.parametrize('name', [
    '../outside', '/absolute', 'C:/absolute', 'folder\\outside', 'folder/../outside',
    'folder//file', 'folder/./file', 'folder/file.', 'folder/file ', 'folder/CON.txt',
    'folder/stream:secret', 'webview/state', '.env', 'folder/.ENV.enterprise',
    backup.INCOMPLETE,
])
def test_unsafe_archive_names_are_rejected_before_destination_creation(tmp_path, name):
    store, _ = workspace(tmp_path)
    archive = tmp_path / 'backup.zip'
    backup.create_backup(store.directory, archive)

    def change(files, manifest):
        content = b'bad'
        files['workspace/' + name] = content
        manifest['files'].append({'path': name, 'size': len(content),
                                  'sha256': hashlib.sha256(content).hexdigest()})

    rewrite(archive, change)
    destination = tmp_path / 'restored'
    with pytest.raises(ValueError):
        backup.restore_backup(archive, destination)
    assert not destination.exists()
    assert not (tmp_path / 'outside').exists()


@pytest.mark.parametrize('damage', ['content', 'missing', 'extra', 'size', 'format', 'case'])
def test_corrupt_or_undeclared_content_is_rejected(tmp_path, damage):
    store, _ = workspace(tmp_path)
    archive = tmp_path / 'backup.zip'
    backup.create_backup(store.directory, archive)

    def change(files, manifest):
        row = next(row for row in manifest['files'] if row['path'].endswith('summary.txt'))
        name = 'workspace/' + row['path']
        if damage == 'content':
            files[name] = b'x' * len(files[name])
        elif damage == 'missing':
            del files[name]
        elif damage == 'extra':
            files['workspace/undeclared'] = b'x'
        elif damage == 'size':
            row['size'] += 1
        elif damage == 'format':
            manifest['format'] = 'future/999'
        else:
            duplicate = dict(row, path=row['path'].upper())
            manifest['files'].append(duplicate)
            files['workspace/' + duplicate['path']] = files[name]

    rewrite(archive, change)
    with pytest.raises(ValueError):
        backup.verify_backup(archive)


def test_zip_symlink_and_duplicate_members_are_rejected(tmp_path):
    store, _ = workspace(tmp_path)
    archive = tmp_path / 'backup.zip'
    backup.create_backup(store.directory, archive)
    name = 'workspace/runs/test/summary.txt'
    with zipfile.ZipFile(archive) as bundle:
        rows = [(info, bundle.read(info)) for info in bundle.infolist()]
    with zipfile.ZipFile(archive, 'w') as bundle:
        for info, content in rows:
            if info.filename == name:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(info, content)
    with pytest.raises(ValueError, match='类型'):
        backup.verify_backup(archive)
    with zipfile.ZipFile(archive, 'a') as bundle, pytest.warns(UserWarning, match='Duplicate'):
        bundle.writestr('manifest.json', b'{}')
    with pytest.raises(ValueError, match='重复'):
        backup.verify_backup(archive)


def test_corrupt_database_is_rejected_even_with_matching_manifest_hash(tmp_path):
    archive = tmp_path / 'backup.zip'
    content = b'not a database'
    manifest = {'format': backup.FORMAT, 'files': [{'path': 'workspace.sqlite3',
                 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}]}
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('manifest.json', json.dumps(manifest))
        bundle.writestr('workspace/workspace.sqlite3', content)
    import sqlite3
    with pytest.raises(sqlite3.Error):
        backup.verify_backup(archive)


def test_limits_and_failed_publish_leave_no_archive(tmp_path, monkeypatch):
    store, _ = workspace(tmp_path)
    archive = tmp_path / 'backup.zip'
    with monkeypatch.context() as scoped:
        scoped.setattr(backup, 'MAX_FILES', 1)
        with pytest.raises(ValueError, match='数量'):
            backup.create_backup(store.directory, archive)
    with monkeypatch.context() as scoped:
        scoped.setattr(backup, 'MAX_BYTES', 1)
        with pytest.raises(ValueError, match='GiB'):
            backup.create_backup(store.directory, archive)
    assert not archive.exists()
    original = backup.os.link

    def concurrent_target(source, destination):
        Path(destination).write_bytes(b'concurrent owner')
        original(source, destination)

    monkeypatch.setattr(backup.os, 'link', concurrent_target)
    with pytest.raises(FileExistsError):
        backup.create_backup(store.directory, archive)
    assert archive.read_bytes() == b'concurrent owner'
    assert not list(tmp_path.glob('tmp*'))


def test_failed_restore_keeps_incomplete_marker_and_launch_refuses_it(tmp_path, monkeypatch):
    store, _ = workspace(tmp_path)
    archive = tmp_path / 'backup.zip'
    backup.create_backup(store.directory, archive)
    destination = tmp_path / 'restored'

    def fail(*args):
        raise OSError('synthetic full disk')

    monkeypatch.setattr(backup.shutil, 'copyfileobj', fail)
    with pytest.raises(OSError, match='full disk'):
        backup.restore_backup(archive, destination)
    assert (destination / backup.INCOMPLETE).is_file()
    from tradingagents.desktop.app import main
    with pytest.raises(SystemExit, match='恢复未完成'):
        main(['--data-dir', str(destination)])


def test_cli_roundtrip_and_errors(tmp_path, capsys):
    store, _ = workspace(tmp_path)
    archive = tmp_path / 'backup.zip'
    backup.main(['create', str(store.directory), str(archive)])
    backup.main(['verify', str(archive)])
    backup.main(['restore', str(archive), str(tmp_path / 'restored')])
    assert len([line for line in capsys.readouterr().out.splitlines()
                if json.loads(line)['status'] == 'ok']) == 3
    with pytest.raises(SystemExit) as error:
        backup.main(['restore', str(archive), str(store.directory)])
    assert error.value.code == 1


def test_unreadable_directory_aborts_backup_without_publishing(tmp_path, monkeypatch):
    store, _ = workspace(tmp_path)

    def unreadable(root, *, followlinks, onerror):
        onerror(PermissionError('synthetic unreadable directory'))
        return iter(())

    monkeypatch.setattr(backup.os, 'walk', unreadable)
    archive = tmp_path / 'backup.zip'
    with pytest.raises(PermissionError, match='unreadable'):
        backup.create_backup(store.directory, archive)
    assert not archive.exists()
