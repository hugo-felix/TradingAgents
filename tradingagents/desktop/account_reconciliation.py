"""Immutable comparisons of caller-declared statements with local account ledgers."""

import argparse
import csv
import io
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tradingagents.desktop.account_events import KINDS, AccountEventLedger, normalize
from tradingagents.desktop.store import now
from tradingagents.desktop.trades import (
    COLUMNS,
    MAX_BYTES,
    MAX_ROWS,
    TradeLedger,
    _fingerprint,
    _text,
    parse_csv,
)

CATEGORIES = ('actual_fill', *KINDS)


def _instant(value):
    value = datetime.fromisoformat(_text(value, 'timestamp'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('对账时间必须含时区。')
    return value.astimezone(UTC).isoformat()


def _entry(kind, payload):
    if kind == 'actual_fill':
        if (not isinstance(payload, dict) or set(payload) != set(COLUMNS)
                or any(not isinstance(value, str) for value in payload.values())):
            raise ValueError('成交字段必须与 CSV 模板完全一致且为字符串。')
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, COLUMNS)
        writer.writeheader()
        writer.writerow(payload)
        row = parse_csv(output.getvalue())[0]
        return {'identity': 'fill:' + row['trade_id'], 'kind': kind, 'payload': row['fill']}
    payload = normalize(kind, payload)
    identity = _fingerprint({key: payload[key] for key in ('broker', 'account_ref', 'event_id')})
    return {'identity': 'event:' + identity, 'kind': kind, 'payload': payload}


def _in_scope(entry, scope):
    payload = entry['payload']
    timestamp = payload['executed_at' if entry['kind'] == 'actual_fill' else 'occurred_at']
    return (entry['kind'] in scope['categories']
            and all(payload[key] == scope[key] for key in ('broker', 'account_ref'))
            and datetime.fromisoformat(scope['period_start']) <= datetime.fromisoformat(timestamp)
            < datetime.fromisoformat(scope['period_end']))


def _statement(statement):
    keys = {'broker', 'account_ref', 'period_start', 'period_end', 'source_label',
            'categories', 'entries'}
    if not isinstance(statement, dict) or set(statement) != keys:
        raise ValueError('对账单字段必须与模板完全一致。')
    if len(json.dumps(statement, ensure_ascii=False).encode('utf-8')) > MAX_BYTES:
        raise ValueError('对账单不得超过 8 MiB。')
    scope = {key: _text(statement[key], key) for key in ('broker', 'account_ref', 'source_label')}
    scope.update({key: _instant(statement[key]) for key in ('period_start', 'period_end')})
    if datetime.fromisoformat(scope['period_start']) >= datetime.fromisoformat(scope['period_end']):
        raise ValueError('对账起点必须早于终点。')
    categories = statement['categories']
    if (not isinstance(categories, list) or not categories
            or any(not isinstance(kind, str) or kind not in CATEGORIES for kind in categories)
            or len(categories) != len(set(categories))):
        raise ValueError('对账类型必须明确列出且不重复。')
    scope['categories'] = categories[:]
    rows = statement['entries']
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError('对账条目必须为列表且不超过 10000 条。')
    entries = []
    for ordinal, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != {'kind', 'payload'}:
            raise ValueError('条目必须包含 kind 和 payload。')
        entry = _entry(row['kind'], row['payload'])
        if not _in_scope(entry, scope):
            raise ValueError('对账条目超出声明的账户、时间或类型范围。')
        entries.append({**entry, 'ordinal': ordinal})
    return scope, entries


def _contents(entry):
    return _fingerprint({'kind': entry['kind'], 'payload': entry['payload']})


class AccountReconciliation:
    def __init__(self, store):
        self.store = store
        AccountEventLedger(store)
        TradeLedger(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS account_reconciliations (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, document TEXT NOT NULL)''')

    def record(self, statement, request_id, recorded_by):
        request_id, recorded_by = (_text(request_id, 'request_id'),
                                   _text(recorded_by, 'recorded_by'))
        scope, external = _statement(statement)
        fingerprint = _fingerprint({'statement': statement, 'recorded_by': recorded_by})
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT fingerprint, document FROM account_reconciliations'
                                  ' WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise ValueError('对账请求标识已用于其他内容或操作人。')
                return json.loads(existing[1])
            local, excluded = [], []
            for row in db.execute('SELECT document FROM actual_fills ORDER BY rowid'):
                document = json.loads(row[0])
                entry = {'identity': 'fill:' + document['trade_id'], 'kind': 'actual_fill',
                         'payload': document['fill']}
                if _in_scope(entry, scope):
                    local.append({**entry, 'document': document})
            for row in db.execute('SELECT document FROM account_events ORDER BY rowid'):
                document = AccountEventLedger._view(db, json.loads(row[0]))
                entry = {'identity': 'event:' + document['record_id'], 'kind': document['kind'],
                         'payload': document['payload']}
                if _in_scope(entry, scope):
                    target = excluded if document['ledger_status'] == 'voided' else local
                    target.append({**entry, 'document': document})
            grouped = {}
            for entry in external:
                grouped.setdefault(entry['identity'], []).append(entry)
            remaining = {entry['identity']: entry for entry in local}
            result = {key: [] for key in ('matched', 'missing_from_ledger',
                      'missing_from_statement', 'mismatched', 'duplicates', 'conflicts')}
            for identity, entries in grouped.items():
                counterpart = remaining.pop(identity, None)
                hashes = {_contents(entry) for entry in entries}
                if len(entries) > 1:
                    result['duplicates' if len(hashes) == 1 else 'conflicts'].append({
                        'identity': identity, 'entries': entries, 'local': counterpart})
                if len(hashes) > 1:
                    continue
                entry = entries[0]
                if counterpart is None:
                    result['missing_from_ledger'].append(entry)
                elif _contents(entry) != _contents(counterpart):
                    result['mismatched'].append({'statement': entry, 'local': counterpart})
                else:
                    result['matched'].append(identity)
            result['missing_from_statement'] = list(remaining.values())
            uncertainties = []
            for side, entries in (('statement', external), ('local', local)):
                for entry in entries:
                    for field in ('fees', 'tax'):
                        if field in entry['payload'] and entry['payload'][field] is None:
                            uncertainties.append({'side': side, 'identity': entry['identity'],
                                                  'field': field})
            document = {'request_id': request_id, 'recorded_by': recorded_by, 'recorded_at': now(),
                        'fingerprint': fingerprint, 'statement': statement, 'scope': scope,
                        'normalized_entries': external, 'local_entries': local,
                        'excluded_voided': excluded, 'local_sha256': _fingerprint([local, excluded]),
                        'result': result, 'uncertainties': uncertainties,
                        'status': ('differences_found' if any(value for key, value in result.items()
                                   if key != 'matched') else 'no_differences_in_declared_scope'),
                        'verification': 'caller_declared_unverified', 'effects': 'not_applied',
                        'account_completeness': 'not_established', 'performance': 'not_evaluated'}
            serialized = json.dumps(document, ensure_ascii=False)
            db.execute('INSERT INTO account_reconciliations VALUES (?, ?, ?)',
                       (request_id, fingerprint, serialized))
            return json.loads(serialized)

    def get(self, request_id):
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM account_reconciliations WHERE request_id=?',
                             (_text(request_id, 'request_id'),)).fetchone()
        if not row:
            raise ValueError('未找到对账快照。')
        return json.loads(row[0])

    def list(self, *, limit=100, offset=0):
        if (type(limit) is not int or not 1 <= limit <= 1000
                or type(offset) is not int or offset < 0):
            raise ValueError('对账分页参数无效。')
        with self.store.connect() as db:
            db.execute('BEGIN')
            total = db.execute('SELECT COUNT(*) FROM account_reconciliations').fetchone()[0]
            rows = db.execute('SELECT document FROM account_reconciliations'
                              ' ORDER BY rowid DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
        return {'total': total, 'items': [json.loads(row[0]) for row in rows]}


def main(argv=None):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock
    from tradingagents.desktop.store import DesktopStore

    parser = argparse.ArgumentParser(description='StockSeek 离线对账，不调整账户或计算收益')
    parser.add_argument('--workspace', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    record = sub.add_parser('record')
    record.add_argument('json', type=Path)
    record.add_argument('--request-id', required=True)
    record.add_argument('--recorded-by', required=True)
    get = sub.add_parser('get')
    get.add_argument('request_id')
    listing = sub.add_parser('list')
    listing.add_argument('--limit', type=int, default=100)
    listing.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if not (args.workspace / 'workspace.sqlite3').is_file():
            raise ValueError('工作空间数据库不存在。')
        if (args.workspace / INCOMPLETE).exists():
            raise ValueError('不能打开未完整恢复的工作空间。')
        with WorkspaceLock(args.workspace):
            ledger = AccountReconciliation(DesktopStore(args.workspace))
            if args.action == 'record':
                with args.json.open('rb') as source:
                    content = source.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ValueError('对账单不得超过 8 MiB。')
                result = ledger.record(json.loads(content.decode('utf-8-sig')),
                                       args.request_id, args.recorded_by)
            elif args.action == 'get':
                result = ledger.get(args.request_id)
            else:
                result = ledger.list(limit=args.limit, offset=args.offset)
    except (OSError, ValueError, OverflowError, sqlite3.Error) as exc:
        parser.exit(1, f'操作失败：{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
