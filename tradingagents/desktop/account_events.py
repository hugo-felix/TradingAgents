"""Caller-declared account events, retained independently of holdings and returns."""

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tradingagents.desktop.store import now, valid_ticker
from tradingagents.desktop.trades import _decimal, _fingerprint, _text

KINDS = ('cash_transfer', 'cash_dividend', 'split')
COMMON = {'broker', 'account_ref', 'event_id', 'occurred_at', 'source_label'}
SPECIFIC = {
    'cash_transfer': {'currency', 'direction', 'amount'},
    'cash_dividend': {'ticker', 'currency', 'gross_amount', 'tax'},
    'split': {'ticker', 'ratio_before', 'ratio_after'},
}


def normalize(kind, payload):
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError('账户事件类型无效。')
    if not isinstance(payload, dict) or set(payload) != COMMON | SPECIFIC[kind]:
        raise ValueError('事件字段必须与对应模板完全一致。')
    result = {key: _text(payload[key], key) for key in COMMON}
    occurred = datetime.fromisoformat(result['occurred_at'])
    if occurred.tzinfo is None or occurred.utcoffset() is None:
        raise ValueError('事件时间必须含时区。')
    result['occurred_at'] = occurred.astimezone(UTC).isoformat()
    if 'ticker' in SPECIFIC[kind]:
        result['ticker'] = valid_ticker(_text(payload['ticker'], 'ticker'))
    if 'currency' in SPECIFIC[kind]:
        currency = _text(payload['currency'], 'currency')
        if not re.fullmatch(r'[A-Z]{3}', currency):
            raise ValueError('币种必须为三个大写字母。')
        result['currency'] = currency
    if kind == 'cash_transfer':
        direction = _text(payload['direction'], 'direction')
        if direction not in ('deposit', 'withdrawal'):
            raise ValueError('现金方向必须为 deposit 或 withdrawal。')
        result['direction'] = direction
        result['amount'] = _decimal(_text(payload['amount'], 'amount'), 'amount', positive=True)
    elif kind == 'cash_dividend':
        result['gross_amount'] = _decimal(_text(payload['gross_amount'], 'gross_amount'),
                                          'gross_amount', positive=True)
        result['tax'] = (None if payload['tax'] is None else
                         _decimal(_text(payload['tax'], 'tax'), 'tax', positive=False))
    else:
        for field in ('ratio_before', 'ratio_after'):
            result[field] = _decimal(_text(payload[field], field), field, positive=True)
    return result


class AccountEventLedger:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS account_events (
                record_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
                broker TEXT NOT NULL, account_ref TEXT NOT NULL, ticker TEXT,
                document TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS account_event_voids (
                record_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL, document TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS account_event_corrections (
                record_id TEXT PRIMARY KEY, replacement_id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                document TEXT NOT NULL)''')

    @staticmethod
    def _view(db, document):
        row = db.execute('SELECT document FROM account_event_voids WHERE record_id=?',
                         (document['record_id'],)).fetchone()
        outgoing = db.execute('SELECT replacement_id FROM account_event_corrections '
                              'WHERE record_id=?', (document['record_id'],)).fetchone()
        incoming = db.execute('SELECT record_id FROM account_event_corrections '
                              'WHERE replacement_id=?', (document['record_id'],)).fetchone()
        return {**document, 'ledger_status': 'voided' if row else 'recorded',
                'void': json.loads(row[0]) if row else None,
                'replaced_by': outgoing[0] if outgoing else None,
                'replaces': incoming[0] if incoming else None}

    def correct(self, record_id, request_id, reason, recorded_by, kind, payload):
        fields = {key: _text(value, key) for key, value in {
            'record_id': record_id, 'request_id': request_id,
            'reason': reason, 'recorded_by': recorded_by}.items()}
        normalized = normalize(kind, payload)
        fingerprint = _fingerprint({**fields, 'kind': kind, 'payload': normalized})
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT fingerprint, document FROM account_event_corrections '
                                  'WHERE request_id=?', (fields['request_id'],)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('更正请求标识已有不同内容；不能覆盖更正历史。')
                return {**json.loads(previous['document']), 'already_recorded': True}
            original = db.execute('SELECT broker, account_ref FROM account_events '
                                  'WHERE record_id=?', (fields['record_id'],)).fetchone()
            if not original:
                raise ValueError('未找到账户事件。')
            if (original['broker'], original['account_ref']) != (
                    normalized['broker'], normalized['account_ref']):
                raise ValueError('更正必须属于原券商及账户。')
            if db.execute('SELECT 1 FROM account_event_voids WHERE record_id=? OR request_id=?',
                          (fields['record_id'], fields['request_id'])).fetchone():
                raise ValueError('原事件已作废或请求标识已使用；不能更正。')
            replacement = self._record(db, kind, payload)
            if replacement['already_recorded']:
                raise ValueError('更正必须使用尚未登记的新事件标识。')
            replacement_id = replacement['record_id']
            void = {**fields, 'recorded_at': now(), 'fingerprint': _fingerprint(fields),
                    'verification': 'caller_declared_unverified', 'effects': 'not_applied',
                    'replacement_id': replacement_id}
            db.execute('INSERT INTO account_event_voids VALUES (?, ?, ?, ?)',
                       (fields['record_id'], fields['request_id'], void['fingerprint'],
                        json.dumps(void, ensure_ascii=False)))
            document = {**fields, 'replacement_id': replacement_id, 'recorded_at': now(),
                        'fingerprint': fingerprint, 'verification': 'caller_declared_unverified',
                        'effects': 'not_applied', 'performance': 'not_evaluated'}
            db.execute('INSERT INTO account_event_corrections VALUES (?, ?, ?, ?, ?)',
                       (fields['record_id'], replacement_id, fields['request_id'], fingerprint,
                        json.dumps(document, ensure_ascii=False)))
        return {**document, 'already_recorded': False}

    def void(self, record_id, request_id, reason, recorded_by):
        fields = {key: _text(value, key) for key, value in {
            'record_id': record_id, 'request_id': request_id,
            'reason': reason, 'recorded_by': recorded_by}.items()}
        fingerprint = _fingerprint(fields)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT fingerprint, document FROM account_event_voids '
                                  'WHERE request_id=?', (fields['request_id'],)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('作废请求标识已有不同内容；不能覆盖作废历史。')
                return {**json.loads(previous['document']), 'already_recorded': True}
            if not db.execute('SELECT 1 FROM account_events WHERE record_id=?',
                              (fields['record_id'],)).fetchone():
                raise ValueError('未找到账户事件。')
            if db.execute('SELECT 1 FROM account_event_voids WHERE record_id=?',
                          (fields['record_id'],)).fetchone():
                raise ValueError('事件已作废；不能重复作废或覆盖原因。')
            document = {**fields, 'recorded_at': now(), 'fingerprint': fingerprint,
                        'verification': 'caller_declared_unverified', 'effects': 'not_applied'}
            db.execute('INSERT INTO account_event_voids VALUES (?, ?, ?, ?)',
                       (fields['record_id'], fields['request_id'], fingerprint,
                        json.dumps(document, ensure_ascii=False)))
        return {**document, 'already_recorded': False}

    def record(self, kind, payload):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._record(db, kind, payload)

    def _record(self, db, kind, payload):
        normalized = normalize(kind, payload)
        identity = {key: normalized[key] for key in ('broker', 'account_ref', 'event_id')}
        record_id = _fingerprint(identity)
        fingerprint = _fingerprint({'kind': kind, 'payload': normalized})
        # Keep precisely the submitted JSON, separate from normalized amounts and UTC times.
        raw = json.loads(json.dumps(payload, ensure_ascii=False))
        previous = db.execute('SELECT fingerprint, document FROM account_events '
                              'WHERE record_id=?', (record_id,)).fetchone()
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise ValueError('同一券商账户事件标识已有不同内容；不能覆盖历史事件。')
            return {**self._view(db, json.loads(previous['document'])), 'already_recorded': True}
        document = {'record_id': record_id, 'kind': kind, 'recorded_at': now(),
                    'payload': normalized, 'raw': raw, 'fingerprint': fingerprint,
                    'verification': 'caller_declared_unverified',
                    'account_completeness': 'not_established', 'effects': 'not_applied',
                    'performance': 'not_evaluated'}
        db.execute('INSERT INTO account_events VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (record_id, fingerprint, kind, normalized['broker'],
                    normalized['account_ref'], normalized.get('ticker'),
                    json.dumps(document, ensure_ascii=False)))
        result = self._view(db, document)
        return {**result, 'already_recorded': False}

    def get(self, record_id):
        record_id = _text(record_id, 'record_id')
        with self.store.connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT document FROM account_events WHERE record_id=?',
                             (record_id,)).fetchone()
            if not row:
                raise ValueError('未找到账户事件。')
            return self._view(db, json.loads(row[0]))

    def list(self, broker=None, account_ref=None, kind=None, *, limit=100, offset=0):
        if (type(limit) is not int or not 1 <= limit <= 1000
                or type(offset) is not int or offset < 0):
            raise ValueError('事件分页参数无效。')
        if kind is not None and (not isinstance(kind, str) or kind not in KINDS):
            raise ValueError('账户事件类型无效。')
        broker = None if broker is None else _text(broker, 'broker')
        account_ref = None if account_ref is None else _text(account_ref, 'account_ref')
        where = (' WHERE (? IS NULL OR broker=?) AND (? IS NULL OR account_ref=?)'
                 ' AND (? IS NULL OR kind=?)')
        params = (broker, broker, account_ref, account_ref, kind, kind)
        with self.store.connect() as db:
            db.execute('BEGIN')
            total = db.execute('SELECT COUNT(*) FROM account_events' + where, params).fetchone()[0]
            rows = db.execute('SELECT document FROM account_events' + where
                              + ' ORDER BY rowid DESC LIMIT ? OFFSET ?',
                              (*params, limit, offset)).fetchall()
            items = [self._view(db, json.loads(row[0])) for row in rows]
        return {'total': total, 'items': items,
                'account_completeness': 'not_established', 'performance': 'not_evaluated'}


def main(argv=None):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock
    from tradingagents.desktop.store import DesktopStore

    parser = argparse.ArgumentParser(description='StockSeek 离线账户事件，不计算收益或改写持仓')
    parser.add_argument('--workspace', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    record = sub.add_parser('record')
    record.add_argument('kind', choices=KINDS)
    record.add_argument('json', type=Path)
    void = sub.add_parser('void')
    void.add_argument('json', type=Path)
    correct = sub.add_parser('correct')
    correct.add_argument('json', type=Path)
    get = sub.add_parser('get')
    get.add_argument('record_id')
    listing = sub.add_parser('list')
    listing.add_argument('--broker')
    listing.add_argument('--account-ref')
    listing.add_argument('--kind', choices=KINDS)
    listing.add_argument('--limit', type=int, default=100)
    listing.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        if not (args.workspace / 'workspace.sqlite3').is_file():
            raise ValueError('工作空间数据库不存在，不会创建新工作空间。')
        if (args.workspace / INCOMPLETE).exists():
            raise ValueError('不能打开未完整恢复的工作空间。')
        with WorkspaceLock(args.workspace):
            ledger = AccountEventLedger(DesktopStore(args.workspace))
            if args.action in ('record', 'void', 'correct'):
                with args.json.open('rb') as source:
                    content = source.read(65537)
                if len(content) > 65536:
                    raise ValueError('事件 JSON 不得超过 64 KiB。')
                payload = json.loads(content.decode('utf-8-sig'))
                if args.action in ('void', 'correct'):
                    expected = {'record_id', 'request_id', 'reason', 'recorded_by'}
                    if args.action == 'correct':
                        expected |= {'kind', 'payload'}
                    if not isinstance(payload, dict) or set(payload) != expected:
                        raise ValueError('作废或更正字段必须与模板完全一致。')
                    result = getattr(ledger, args.action)(**payload)
                else:
                    result = ledger.record(args.kind, payload)
            elif args.action == 'get':
                result = ledger.get(args.record_id)
            else:
                result = ledger.list(args.broker, args.account_ref, args.kind,
                                     limit=args.limit, offset=args.offset)
    except (OSError, ValueError, OverflowError, sqlite3.Error) as exc:
        parser.exit(1, f'操作失败：{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
