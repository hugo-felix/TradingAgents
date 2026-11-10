"""Caller-authored reviews and prospective personal rule drafts, without model inference."""

import json
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
