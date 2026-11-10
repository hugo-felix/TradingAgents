"""Caller-authored reviews and prospective personal rule drafts, without model inference."""

import argparse
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from tradingagents.desktop.store import now
from tradingagents.desktop.strategies import StrategyRegistry, validate_definition
from tradingagents.desktop.trade_contexts import TradeContextRegistry
from tradingagents.desktop.trades import _fingerprint, _text

DIMENSIONS = {'research', 'strategy', 'execution', 'uncertainty'}


class ReviewRegistry:
    def __init__(self, store):
        self.store = store
        self.contexts = TradeContextRegistry(store)
        self.strategies = StrategyRegistry(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS personal_trade_reviews (
                review_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL, document TEXT NOT NULL)''')

    def record(self, trade_id, notes, request_id):
        trade_id = _text(trade_id, 'trade_id')
        request_id = _text(request_id, 'request_id')
        if not isinstance(notes, dict) or set(notes) != DIMENSIONS:
            raise ValueError('复盘必须分别填写 research、strategy、execution 和 uncertainty。')
        notes = {key: None if value is None and key != 'uncertainty'
                 else _text(value, key, limit=10000) for key, value in notes.items()}
        fingerprint = _fingerprint({'trade_id': trade_id, 'notes': notes})
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT fingerprint, document FROM personal_trade_reviews '
                             'WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise ValueError('复盘请求标识已有不同内容，不能覆盖历史复盘。')
                return json.loads(row['document'])
            row = db.execute('SELECT document FROM actual_fills WHERE trade_id=?',
                             (trade_id,)).fetchone()
            if not row:
                raise ValueError('未找到实际成交。')
            fill = json.loads(row[0])
            context = db.execute('SELECT document FROM actual_fill_contexts WHERE trade_id=? '
                                 'ORDER BY version DESC LIMIT 1', (trade_id,)).fetchone()
            document = {'review_id': uuid4().hex, 'request_id': request_id,
                        'recorded_at': now(), 'trade_id': trade_id, 'notes': notes,
                        'fill_snapshot': fill,
                        'context_snapshot': json.loads(context[0]) if context else None,
                        'assessment': 'personal_opinion', 'causation': 'not_established',
                        'historical_knowledge': 'not_established', 'fingerprint': fingerprint}
            db.execute('INSERT INTO personal_trade_reviews VALUES (?, ?, ?, ?)',
                       (document['review_id'], request_id, fingerprint,
                        json.dumps(document, ensure_ascii=False)))
        return document

    def get(self, review_id):
        review_id = _text(review_id, 'review_id')
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM personal_trade_reviews WHERE review_id=?',
                             (review_id,)).fetchone()
        if not row:
            raise ValueError('未找到个人复盘。')
        return json.loads(row[0])

    def list(self, trade_id=None, *, limit=100, offset=0):
        if (type(limit) is not int or not 1 <= limit <= 1000
                or type(offset) is not int or offset < 0):
            raise ValueError('复盘分页参数无效。')
        if trade_id is not None:
            trade_id = _text(trade_id, 'trade_id')
        where = 'WHERE (? IS NULL OR json_extract(document, \'$.trade_id\')=?)'
        with self.store.connect() as db:
            db.execute('BEGIN')
            total = db.execute('SELECT COUNT(*) FROM personal_trade_reviews ' + where,
                               (trade_id, trade_id)).fetchone()[0]
            rows = db.execute('SELECT document FROM personal_trade_reviews ' + where
                              + ' ORDER BY rowid DESC LIMIT ? OFFSET ?',
                              (trade_id, trade_id, limit, offset)).fetchall()
        return {'total': total, 'items': [json.loads(row[0]) for row in rows]}

    def create_draft(self, review_id, definition):
        review = self.get(review_id)
        definition = validate_definition(definition)
        if not any(definition.get('rules', {}).values()):
            raise ValueError('请明确填写从复盘提出的个人规则。')
        # A review cannot confer author provenance or confirmation on a proposed rule.
        for rows in definition['rules'].values():
            for row in rows:
                row.update(kind='personal', state='pending',
                           citation=f"personal-review:{review['review_id']}; {row['citation']}")
        definition['source'] = f"个人复盘 {review['review_id']}"
        definition['review_origin'] = {'snapshot': review, 'sha256': _fingerprint(review),
                                     'usage': 'prospective_only'}
        # One initial draft per review; duplicates and concurrent writers fail closed.
        # Later edits and confirmation use the existing append-only strategy workflow.
        return self.strategies.save(definition, strategy_id='review-' + review['review_id'])


def main(argv=None):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock
    from tradingagents.desktop.store import DesktopStore

    parser = argparse.ArgumentParser(description='StockSeek 离线个人复盘与待确认规则草案')
    parser.add_argument('--workspace', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    record = sub.add_parser('record')
    record.add_argument('trade_id')
    record.add_argument('json', type=Path)
    record.add_argument('--request-id', required=True)
    get = sub.add_parser('get')
    get.add_argument('review_id')
    listing = sub.add_parser('list')
    listing.add_argument('--trade-id')
    listing.add_argument('--limit', type=int, default=100)
    listing.add_argument('--offset', type=int, default=0)
    draft = sub.add_parser('draft')
    draft.add_argument('review_id')
    draft.add_argument('json', type=Path)
    args = parser.parse_args(argv)
    try:
        if not (args.workspace / 'workspace.sqlite3').is_file():
            raise ValueError('工作空间数据库不存在，不会创建新工作空间。')
        if (args.workspace / INCOMPLETE).exists():
            raise ValueError('不能打开未完整恢复的工作空间。')
        with WorkspaceLock(args.workspace):
            registry = ReviewRegistry(DesktopStore(args.workspace))
            if args.action in ('record', 'draft'):
                with args.json.open('rb') as source:
                    content = source.read(65537)
                if len(content) > 65536:
                    raise ValueError('复盘或规则 JSON 不得超过 64 KiB。')
                payload = json.loads(content.decode('utf-8-sig'))
                result = (registry.record(args.trade_id, payload, args.request_id)
                          if args.action == 'record'
                          else registry.create_draft(args.review_id, payload))
            elif args.action == 'get':
                result = registry.get(args.review_id)
            else:
                result = registry.list(args.trade_id, limit=args.limit, offset=args.offset)
    except (OSError, ValueError, OverflowError, sqlite3.Error) as exc:
        parser.exit(1, f'复盘操作失败：{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
