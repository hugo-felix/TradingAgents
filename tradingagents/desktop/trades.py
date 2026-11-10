"""Append-only, caller-declared actual fills; independent of holdings and plans."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from tradingagents.desktop.store import now, valid_ticker

COLUMNS = ('broker', 'account_ref', 'execution_id', 'trade_date', 'executed_at', 'ticker',
           'market', 'side', 'quantity', 'price', 'currency', 'fees')
MAX_BYTES = 8 * 1024**2
MAX_ROWS = 10000


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _text(value, field, limit=200):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 for c in value)):
        raise ValueError(f'成交字段无效：{field}')
    return value.strip()


def _decimal(value, field, *, positive):
    if len(value) > 100 or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value):
        raise ValueError(f'成交数值无效：{field}')
    number = Decimal(value)
    if positive and number <= 0:
        raise ValueError(f'成交数值必须大于零：{field}')
    result = format(number, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def parse_csv(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('CSV 必须是文本，且不超过 8 MiB。')
    reader = csv.DictReader(io.StringIO(text.removeprefix('\ufeff'), newline=''), strict=True)
    rows = []
    try:
        if (not reader.fieldnames or len(reader.fieldnames) != len(COLUMNS)
                or set(reader.fieldnames) != set(COLUMNS)):
            raise ValueError('CSV 列名必须与标准成交模板完全一致，不能重复。')
        for ordinal, raw in enumerate(reader, 1):
            if ordinal > MAX_ROWS:
                raise ValueError('单批成交超过 10000 行上限。')
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError(f'第 {ordinal} 条成交列数不匹配。')
            row = {key: _text(raw[key], key) for key in COLUMNS if key != 'fees'}
            local = datetime.fromisoformat(row['executed_at'])
            if local.tzinfo is None or local.utcoffset() is None:
                raise ValueError('成交时间必须包含时区。')
            if date.fromisoformat(row['trade_date']).isoformat() != row['trade_date']:
                raise ValueError('交易日必须使用 YYYY-MM-DD。')
            if row['trade_date'] != local.date().isoformat():
                raise ValueError('交易日与成交时间的本地日期不一致。')
            row['executed_at'] = local.astimezone(UTC).isoformat()
            row['ticker'] = valid_ticker(row['ticker'])
            if row['side'] not in {'buy', 'sell'}:
                raise ValueError('成交方向必须是 buy 或 sell。')
            if not re.fullmatch(r'[A-Z]{3}', row['currency']):
                raise ValueError('币种必须是三个大写字母，并与费用币种一致。')
            row['quantity'] = _decimal(row['quantity'], 'quantity', positive=True)
            row['price'] = _decimal(row['price'], 'price', positive=True)
            row['fees'] = (None if not raw['fees'].strip()
                           else _decimal(raw['fees'].strip(), 'fees', positive=False))
            identity = {key: row[key] for key in ('broker', 'account_ref', 'trade_date',
                                                 'execution_id')}
            rows.append({'ordinal': ordinal, 'raw': raw, 'trade_id': _fingerprint(identity),
                         'fill': row, 'fingerprint': _fingerprint(row)})
    except (csv.Error, OverflowError) as exc:
        raise ValueError('CSV 无法解析或成交时间超出支持范围。') from exc
    if not rows:
        raise ValueError('CSV 没有成交记录。')
    # Repeated broker IDs with different economics are conflicts, never updates.
    seen = {}
    for row in rows:
        previous = seen.setdefault(row['trade_id'], row['fingerprint'])
        if previous != row['fingerprint']:
            raise ValueError('同一批内的成交标识冲突，请核对券商、账户、交易日和逐笔成交号。')
    return rows


class TradeLedger:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS actual_fills (
                    trade_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    executed_at TEXT NOT NULL, ticker TEXT NOT NULL, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actual_fill_imports (
                    batch_id TEXT PRIMARY KEY, document TEXT NOT NULL);
            ''')

    @staticmethod
    def _batch(text, source_label):
        source_label = _text(source_label, 'source_label')
        rows = parse_csv(text)
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        return source_label, digest, rows

    def preview(self, text, source_label):
        source_label, digest, rows = self._batch(text, source_label)
        unique = {}
        conflicts = []
        with self.store.connect() as db:
            for row in rows:
                unique.setdefault(row['trade_id'], row)
            existing = 0
            for trade_id, row in unique.items():
                found = db.execute('SELECT fingerprint FROM actual_fills WHERE trade_id=?',
                                   (trade_id,)).fetchone()
                if found and found[0] != row['fingerprint']:
                    conflicts.append({'ordinal': row['ordinal'], 'trade_id': trade_id})
                elif found:
                    existing += 1
        return {'source_label': source_label, 'sha256': digest, 'rows': len(rows),
                'new': len(unique) - existing - len(conflicts),
                'duplicates': len(rows) - len(unique) + existing, 'conflicts': conflicts,
                'missing_fees': sum(row['fill']['fees'] is None for row in rows),
                'items': rows,
                'kind': 'actual', 'verification': 'caller_declared_unverified'}

    def import_csv(self, text, source_label, expected_sha256):
        source_label, digest, rows = self._batch(text, source_label)
        if expected_sha256 != digest:
            raise ValueError('CSV 已变化，请重新预览并核对指纹。')
        batch_id = _fingerprint({'source_label': source_label, 'sha256': digest})
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT document FROM actual_fill_imports WHERE batch_id=?',
                                  (batch_id,)).fetchone()
            if previous:
                return {**json.loads(previous[0]), 'already_imported': True}
            added = 0
            recorded_at = now()
            for row in rows:
                previous = db.execute('SELECT fingerprint FROM actual_fills WHERE trade_id=?',
                                      (row['trade_id'],)).fetchone()
                if previous:
                    if previous[0] != row['fingerprint']:
                        raise ValueError('已保存的成交标识与本批内容冲突，整批未导入。')
                    continue
                document = {'trade_id': row['trade_id'], 'kind': 'actual', 'fill': row['fill'],
                            'recorded_at': recorded_at, 'first_batch_id': batch_id,
                            'verification': 'caller_declared_unverified'}
                db.execute('INSERT INTO actual_fills VALUES (?, ?, ?, ?, ?)', (
                    row['trade_id'], row['fingerprint'], row['fill']['executed_at'],
                    row['fill']['ticker'], json.dumps(document, ensure_ascii=False)))
                added += 1
            document = {'batch_id': batch_id, 'source_label': source_label, 'sha256': digest,
                        'recorded_at': recorded_at, 'kind': 'actual', 'added': added,
                        'duplicates': len(rows) - added, 'rows': rows, 'csv_text': text,
                        'verification': 'caller_declared_unverified'}
            db.execute('INSERT INTO actual_fill_imports VALUES (?, ?)',
                       (batch_id, json.dumps(document, ensure_ascii=False)))
        return {**document, 'already_imported': False}

    def get_fill(self, trade_id):
        trade_id = _text(trade_id, 'trade_id')
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM actual_fills WHERE trade_id=?',
                             (trade_id,)).fetchone()
        if not row:
            raise ValueError('未找到实际成交。')
        return json.loads(row[0])

    def get_import(self, batch_id):
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM actual_fill_imports WHERE batch_id=?',
                             (batch_id,)).fetchone()
        if not row:
            raise ValueError('未找到成交导入批次。')
        return json.loads(row[0])

    def list_fills(self, ticker=None, *, limit=100, offset=0):
        if (type(limit) is not int or not 1 <= limit <= 1000
                or type(offset) is not int or offset < 0):
            raise ValueError('成交查询分页参数无效。')
        if ticker is not None:
            ticker = valid_ticker(_text(ticker, 'ticker'))
        with self.store.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM actual_fills WHERE (? IS NULL OR ticker=?)',
                               (ticker, ticker)).fetchone()[0]
            rows = db.execute('''SELECT document FROM actual_fills
                WHERE (? IS NULL OR ticker=?) ORDER BY executed_at DESC, trade_id
                LIMIT ? OFFSET ?''', (ticker, ticker, limit, offset)).fetchall()
        return {'total': total, 'items': [json.loads(row[0]) for row in rows]}


def main(argv=None):
    """Explicit offline import; never silently create an empty workspace."""
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock
    from tradingagents.desktop.store import DesktopStore

    parser = argparse.ArgumentParser(description='StockSeek 离线实际成交台账（先预览再导入）')
    parser.add_argument('--workspace', required=True, type=Path)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('preview', 'import'):
        command = sub.add_parser(action)
        command.add_argument('csv', type=Path)
        command.add_argument('--source', required=True)
        if action == 'import':
            command.add_argument('--sha256', required=True)
    listing = sub.add_parser('list')
    listing.add_argument('--ticker')
    listing.add_argument('--limit', type=int, default=100)
    listing.add_argument('--offset', type=int, default=0)
    batch = sub.add_parser('batch')
    batch.add_argument('batch_id')
    single = sub.add_parser('get')
    single.add_argument('trade_id')
    for action in ('context-save', 'context', 'context-history'):
        command = sub.add_parser(action)
        command.add_argument('trade_id')
        if action == 'context-save':
            command.add_argument('json', type=Path)
            command.add_argument('--expected-version', required=True, type=int)
        elif action == 'context':
            command.add_argument('--version', type=int)
        else:
            command.add_argument('--limit', type=int, default=100)
            command.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if not (args.workspace / 'workspace.sqlite3').is_file():
            raise ValueError('工作空间数据库不存在；请核对目录，不会创建新工作空间。')
        if (args.workspace / INCOMPLETE).exists():
            raise ValueError('不能打开未完整恢复的工作空间。')
        with WorkspaceLock(args.workspace):
            ledger = TradeLedger(DesktopStore(args.workspace))
            if args.action in {'preview', 'import'}:
                with args.csv.open('rb') as source:
                    content = source.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ValueError('CSV 不得超过 8 MiB。')
                text = content.decode('utf-8')
                if args.action == 'preview':
                    result = ledger.preview(text, args.source)
                else:
                    result = ledger.import_csv(text, args.source, args.sha256)
                    # Full raw input remains available via the explicit batch query.
                    result = {key: value for key, value in result.items()
                              if key not in {'rows', 'csv_text'}}
            elif args.action == 'list':
                result = ledger.list_fills(args.ticker, limit=args.limit, offset=args.offset)
            elif args.action == 'get':
                result = ledger.get_fill(args.trade_id)
            elif args.action == 'batch':
                result = ledger.get_import(args.batch_id)
            else:
                from tradingagents.desktop.trade_contexts import TradeContextRegistry
                contexts = TradeContextRegistry(ledger.store)
                if args.action == 'context-save':
                    with args.json.open('rb') as source:
                        content = source.read(65537)
                    if len(content) > 65536:
                        raise ValueError('关联 JSON 不得超过 64 KiB。')
                    result = contexts.record(args.trade_id, json.loads(content.decode('utf-8-sig')),
                                             expected_version=args.expected_version)
                elif args.action == 'context':
                    result = contexts.get(args.trade_id, args.version)
                else:
                    result = contexts.history(args.trade_id, limit=args.limit, offset=args.offset)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'操作失败：{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
