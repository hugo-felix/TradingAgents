"""Closed-workspace backups and verified restore into a new directory only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from tradingagents.desktop.instance import WorkspaceLock
from tradingagents.desktop.instruments import canonical_symbol
from tradingagents.desktop.store import now

FORMAT = 'stockseek-backup/1'
INCOMPLETE = 'RESTORE_INCOMPLETE'
MAX_FILES = 100000
MAX_BYTES = 10 * 1024**3
EXCLUDED = {'webview', 'workspace.lock', 'workspace.sqlite3-wal', 'workspace.sqlite3-shm'}


def _safe_name(name):
    parts = PurePosixPath(name).parts
    reserved = {'con', 'prn', 'aux', 'nul',
                *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
    if (not isinstance(name, str) or not parts or '/'.join(parts) != name
            or any(part in {'.', '..'} or part.endswith((' ', '.'))
                   or part.split('.')[0].casefold() in reserved
                   or any(c in part for c in '\\:*?"<>|\x00') for part in parts)
            or name.startswith('/') or parts[0].casefold() in EXCLUDED
            or any(part.casefold().startswith('.env') for part in parts)
            or name == INCOMPLETE):
        raise ValueError('备份文件路径无效或包含排除项。')
    return name


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))


def _files(root):
    def fail(error):
        raise error

    result = []
    for folder, directories, files in os.walk(root, followlinks=False, onerror=fail):
        parent = Path(folder)
        for name in list(directories):
            if name.casefold().startswith('.env') or (parent == root and name.casefold() in EXCLUDED):
                directories.remove(name)
            elif _linked(parent / name):
                raise ValueError('工作空间包含链接或重解析目录，不能安全备份。')
        for name in files:
            if name.casefold().startswith('.env') or (parent == root and name.casefold() in EXCLUDED):
                continue
            path = parent / name
            if _linked(path) or not path.is_file():
                raise ValueError('工作空间包含链接或非普通文件，不能安全备份。')
            result.append((path, _safe_name(path.relative_to(root).as_posix())))
    return sorted(result, key=lambda row: row[1])


def _database_valid(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.create_function('canonical_ticker', 1, canonical_symbol, deterministic=True)
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('备份数据库完整性校验失败。')
        if not {'reports', 'holdings', 'settings'}.issubset({
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}):
            raise ValueError('不是受支持的 StockSeek 工作空间数据库。')


def create_backup(directory, destination):
    root = Path(directory).resolve(strict=True)
    destination = Path(destination).absolute()
    if not root.is_dir() or not (root / 'workspace.sqlite3').is_file():
        raise ValueError('请选择已有 StockSeek 工作空间。')
    if destination.resolve().is_relative_to(root):
        raise ValueError('备份文件必须位于工作空间之外。')
    if destination.exists():
        raise ValueError('备份目标已存在，不会覆盖。')
    with WorkspaceLock(root), tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        if (root / INCOMPLETE).exists():
            raise ValueError('工作空间恢复未完成，不能备份。')
        files = _files(root)
        if len(files) > MAX_FILES:
            raise ValueError('备份文件数量超过上限。')
        stage = Path(temporary)
        database = stage / 'workspace.sqlite3'
        with (closing(sqlite3.connect((root / 'workspace.sqlite3').as_uri() + '?mode=ro',
                                      uri=True)) as src,
              closing(sqlite3.connect(database)) as target):
            src.backup(target)
        _database_valid(database)
        archive = stage / 'backup.zip'
        manifest = {'format': FORMAT, 'created_at': now(), 'files': [],
                    'excluded': sorted(EXCLUDED | {'.env* (at any depth)'})}
        total = 0
        identities = set()
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            for path, name in files:
                identity = unicodedata.normalize('NFC', name).casefold()
                if identity in identities:
                    raise ValueError('文件名在跨平台恢复时存在冲突。')
                identities.add(identity)
                if name == 'workspace.sqlite3':
                    path = database
                digest = hashlib.sha256()
                size = 0
                with path.open('rb') as source, bundle.open('workspace/' + name, 'w') as output:
                    while block := source.read(1024 * 1024):
                        total += len(block)
                        size += len(block)
                        if total > MAX_BYTES:
                            raise ValueError('备份超过 10 GiB 上限。')
                        digest.update(block)
                        output.write(block)
                manifest['files'].append({'path': name, 'size': size, 'sha256': digest.hexdigest()})
            bundle.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        # Same-volume hard link publishes without replacing an existing destination.
        os.link(archive, destination)
    return manifest


def _extract(archive, stage):
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        if len(infos) > MAX_FILES + 1 or len({info.filename for info in infos}) != len(infos):
            raise ValueError('备份有重复项或文件数量超过上限。')
        try:
            info = bundle.getinfo('manifest.json')
            if info.file_size > 16 * 1024**2:
                raise ValueError('备份清单过大。')
            manifest = json.loads(bundle.read(info))
        except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('备份清单缺失或损坏。') from exc
        if (not isinstance(manifest, dict) or manifest.get('format') != FORMAT
                or not isinstance(manifest.get('files'), list)):
            raise ValueError('不支持的备份格式。')
        expected = {'manifest.json'}
        identities = set()
        total = 0
        for row in manifest['files']:
            if (not isinstance(row, dict) or set(row) != {'path', 'size', 'sha256'}
                    or not isinstance(row['path'], str) or type(row['size']) is not int
                    or row['size'] < 0 or not isinstance(row['sha256'], str)
                    or len(row['sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in row['sha256'])):
                raise ValueError('备份文件清单无效。')
            name = _safe_name(row['path'])
            identity = unicodedata.normalize('NFC', name).casefold()
            if identity in identities:
                raise ValueError('备份文件名重复或跨平台冲突。')
            identities.add(identity)
            expected.add('workspace/' + name)
            try:
                info = bundle.getinfo('workspace/' + name)
            except KeyError as exc:
                raise ValueError('备份缺少清单声明的文件。') from exc
            if (info.file_size != row['size']
                    or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)):
                raise ValueError('备份文件大小或类型无效。')
            total += info.file_size
            if total > MAX_BYTES:
                raise ValueError('备份超过 10 GiB 上限。')
            path = stage.joinpath(*PurePosixPath(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with bundle.open(info) as source, path.open('xb') as output:
                while block := source.read(1024 * 1024):
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != row['sha256']:
                raise ValueError('备份文件指纹不匹配。')
        if expected != {info.filename for info in infos} or 'workspace/workspace.sqlite3' not in expected:
            raise ValueError('备份有未声明的内容或缺少数据库。')
        _database_valid(stage / 'workspace.sqlite3')
    return manifest


def verify_backup(archive):
    with tempfile.TemporaryDirectory() as temporary:
        return _extract(archive, Path(temporary))


def restore_backup(archive, directory):
    destination = Path(directory).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('恢复只能使用不存在的新目录，不会覆盖已有工作空间。')
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        stage = Path(temporary)
        manifest = _extract(archive, stage)
        # Reserve exclusively only after the entire archive and database pass validation.
        destination.mkdir()
        marker = destination / INCOMPLETE
        marker.write_text(FORMAT, encoding='utf-8')
        for path, name in _files(stage):
            target = destination.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with path.open('rb') as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
        marker.unlink()
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description='StockSeek 离线工作空间备份与恢复')
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create')
    create.add_argument('workspace', type=Path)
    create.add_argument('archive', type=Path)
    verify = sub.add_parser('verify')
    verify.add_argument('archive', type=Path)
    restore = sub.add_parser('restore')
    restore.add_argument('archive', type=Path)
    restore.add_argument('workspace', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == 'create':
            result = create_backup(args.workspace, args.archive)
        elif args.action == 'verify':
            result = verify_backup(args.archive)
        else:
            result = restore_backup(args.archive, args.workspace)
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, RuntimeError) as exc:
        parser.exit(1, f'操作失败：{exc}\n')
    print(json.dumps({'format': result['format'], 'files': len(result['files']),
                      'status': 'ok'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
