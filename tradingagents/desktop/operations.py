"""Immutable intentions and caller-declared simulations, never actual fills."""

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from tradingagents.desktop.store import now, valid_ticker
from tradingagents.desktop.trades import _decimal, _fingerprint, _text

KINDS = ('plan', 'simulation')
COMMON = {'book_ref', 'ticker', 'market', 'side', 'quantity', 'currency', 'source_label'}
SPECIFIC = {'plan': {'planned_for', 'limit_price', 'reason'},
            'simulation': {'simulated_at', 'price', 'fees', 'assumptions'}}


def normalize(kind, payload):
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError('操作类型只能为 plan 或 simulation，真实成交必须走独立导入。')
    if not isinstance(payload, dict) or set(payload) != COMMON | SPECIFIC[kind]:
        raise ValueError('操作字段必须与对应计划或模拟模板完全一致。')
    result = {key: _text(payload[key], key) for key in COMMON}
    result['ticker'] = valid_ticker(result['ticker'])
    if result['side'] not in ('buy', 'sell'):
        raise ValueError('操作方向必须为 buy 或 sell。')
    if not re.fullmatch(r'[A-Z]{3}', result['currency']):
        raise ValueError('币种必须为三个大写字母。')
    result['quantity'] = _decimal(result['quantity'], 'quantity', positive=True)
    time_key = 'planned_for' if kind == 'plan' else 'simulated_at'
    local = datetime.fromisoformat(_text(payload[time_key], time_key))
    if local.tzinfo is None or local.utcoffset() is None:
        raise ValueError('操作时间必须含时区。')
    result[time_key] = local.astimezone(UTC).isoformat()
    price_key = 'limit_price' if kind == 'plan' else 'price'
    price = payload[price_key]
    result[price_key] = (None if kind == 'plan' and price is None
                         else _decimal(_text(price, price_key), price_key, positive=True))
    if kind == 'plan':
        result['reason'] = _text(payload['reason'], 'reason', limit=10000)
    else:
        fees = payload['fees']
        result['fees'] = (None if fees is None
                          else _decimal(_text(fees, 'fees'), 'fees', positive=False))
        result['assumptions'] = _text(payload['assumptions'], 'assumptions', limit=10000)
    return result


class OperationLedger:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS non_actual_operations (
                operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, ticker TEXT NOT NULL, recorded_at TEXT NOT NULL,
                document TEXT NOT NULL, UNIQUE(kind, request_id))''')

    def record(self, kind, payload, request_id):
        normalized = normalize(kind, payload)
        request_id = _text(request_id, 'request_id')
        fingerprint = _fingerprint(normalized)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT fingerprint, document FROM non_actual_operations
                WHERE kind=? AND request_id=?''', (kind, request_id)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise ValueError('请求标识已有不同内容；不能覆盖历史操作。')
                return {**json.loads(row['document']), 'already_recorded': True}
            document = {'operation_id': uuid4().hex, 'kind': kind, 'request_id': request_id,
                        'recorded_at': now(), 'payload': normalized,
                        'verification': 'caller_declared', 'historical_knowledge': 'not_established',
                        'execution': 'not_executed' if kind == 'plan' else 'simulated_only',
                        'fingerprint': fingerprint}
            db.execute('INSERT INTO non_actual_operations VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (document['operation_id'], kind, request_id, fingerprint,
                        normalized['ticker'], document['recorded_at'],
                        json.dumps(document, ensure_ascii=False)))
        return {**document, 'already_recorded': False}

    def get(self, operation_id):
        operation_id = _text(operation_id, 'operation_id')
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM non_actual_operations WHERE operation_id=?',
                             (operation_id,)).fetchone()
        if not row:
            raise ValueError('未找到计划或模拟记录。')
        return json.loads(row[0])

    def list(self, kind=None, ticker=None, *, limit=100, offset=0):
        if kind is not None and (not isinstance(kind, str) or kind not in KINDS):
            raise ValueError('操作类型无效。')
        if (type(limit) is not int or not 1 <= limit <= 1000
                or type(offset) is not int or offset < 0):
            raise ValueError('操作分页参数无效。')
        if ticker is not None:
            ticker = valid_ticker(_text(ticker, 'ticker'))
        where = 'WHERE (? IS NULL OR kind=?) AND (? IS NULL OR ticker=?)'
        args = (kind, kind, ticker, ticker)
        with self.store.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM non_actual_operations ' + where,
                               args).fetchone()[0]
            rows = db.execute('SELECT document FROM non_actual_operations ' + where
                              + ' ORDER BY recorded_at DESC, operation_id LIMIT ? OFFSET ?',
                              (*args, limit, offset)).fetchall()
        return {'total': total, 'items': [json.loads(row[0]) for row in rows]}


def main(argv=None):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock
    from tradingagents.desktop.store import DesktopStore

    parser = argparse.ArgumentParser(description='StockSeek 离线计划/模拟记录，不执行交易')
    parser.add_argument('--workspace', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    record = sub.add_parser('record')
    record.add_argument('kind', choices=KINDS)
    record.add_argument('json', type=Path)
    record.add_argument('--request-id', required=True)
    get = sub.add_parser('get')
    get.add_argument('operation_id')
    listing = sub.add_parser('list')
    listing.add_argument('--kind', choices=KINDS)
    listing.add_argument('--ticker')
    listing.add_argument('--limit', type=int, default=100)
    listing.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if not (args.workspace / 'workspace.sqlite3').is_file():
            raise ValueError('工作空间数据库不存在，不会创建新工作空间。')
        if (args.workspace / INCOMPLETE).exists():
            raise ValueError('不能打开未完整恢复的工作空间。')
        with WorkspaceLock(args.workspace):
            ledger = OperationLedger(DesktopStore(args.workspace))
            if args.action == 'record':
                with args.json.open('rb') as source:
                    content = source.read(65537)
                if len(content) > 65536:
                    raise ValueError('操作 JSON 不得超过 64 KiB。')
                result = ledger.record(args.kind, json.loads(content.decode('utf-8-sig')),
                                       args.request_id)
            elif args.action == 'get':
                result = ledger.get(args.operation_id)
            else:
                result = ledger.list(args.kind, args.ticker, limit=args.limit, offset=args.offset)
    except (OSError, ValueError, OverflowError, sqlite3.Error) as exc:
        parser.exit(1, f'操作失败：{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
