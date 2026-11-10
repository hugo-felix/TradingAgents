"""Append-only personal annotations, never evidence of historical knowledge or causation."""

import hashlib
import json
from datetime import date, datetime

from tradingagents.desktop.store import now, valid_ticker
from tradingagents.desktop.trades import TradeLedger, _fingerprint, _text

FIELDS = {'personal_reason', 'report_id', 'strategy_id', 'strategy_version'}


def _timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('来源时间缺少时区，不能判定成交时点。')
    return result


def _snapshot(text):
    return {'document_text': text, 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'document': json.loads(text)}


class TradeContextRegistry:
    def __init__(self, store):
        self.store = store
        self.ledger = TradeLedger(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS actual_fill_contexts (
                trade_id TEXT NOT NULL, version INTEGER NOT NULL, recorded_at TEXT NOT NULL,
                document TEXT NOT NULL, PRIMARY KEY(trade_id, version))''')

    def record(self, trade_id, context, *, expected_version=0):
        trade_id = _text(trade_id, 'trade_id')
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('关联版本无效。')
        if not isinstance(context, dict) or set(context) - FIELDS:
            raise ValueError('关联内容必须是对象，且仅含个人理由、报告和策略版本字段。')
        request = {}
        for key in ('personal_reason', 'report_id', 'strategy_id'):
            value = context.get(key)
            request[key] = None if value is None else _text(
                value, key, limit=10000 if key == 'personal_reason' else 200)
        version = context.get('strategy_version')
        if request['strategy_id'] is None:
            if version is not None:
                raise ValueError('策略版本必须与策略标识一起填写。')
        elif type(version) is not int or version < 1:
            raise ValueError('必须指定明确的正整数策略版本，不能自动使用最新版本。')
        request['strategy_version'] = version
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT document FROM actual_fills WHERE trade_id=?',
                             (trade_id,)).fetchone()
            if not row:
                raise ValueError('未找到实际成交。')
            fill = json.loads(row[0])
            latest = db.execute('SELECT MAX(version) FROM actual_fill_contexts WHERE trade_id=?',
                                (trade_id,)).fetchone()[0] or 0
            if latest != expected_version:
                raise ValueError('成交关联已更新，请读取当前版本后重试。')
            executed_at = _timestamp(fill['fill']['executed_at'])
            report = None
            if request['report_id'] is not None:
                row = db.execute('SELECT * FROM reports WHERE id=?',
                                 (request['report_id'],)).fetchone()
                if not row:
                    raise ValueError('未找到报告快照。')
                if valid_ticker(row['ticker']) != fill['fill']['ticker']:
                    raise ValueError('报告与成交的股票代码不一致。')
                if date.fromisoformat(row['date']) > date.fromisoformat(fill['fill']['trade_date']):
                    raise ValueError('成交后的分析日期不能作为当时报告。')
                # Imported historical material is useful for review but does not establish
                # publication time or prove the trader possessed it when the fill occurred.
                report = {'report_id': row['id'], 'analysis_date': row['date'],
                          'imported_at': row['imported_at'], **_snapshot(row['document']),
                          'availability': ('recorded_before_trade'
                                           if _timestamp(row['imported_at']) <= executed_at
                                           else 'not_established')}
            strategy = None
            if request['strategy_id'] is not None:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                    "AND name='strategy_versions'").fetchone()
                row = db.execute('''SELECT * FROM strategy_versions
                    WHERE strategy_id=? AND version=?''',
                    (request['strategy_id'], version)).fetchone() if exists else None
                if not row:
                    raise ValueError('未找到指定策略版本。')
                strategy = _snapshot(row['document'])
                document = strategy['document']
                if document.get('status') != 'confirmed':
                    raise ValueError('只能关联已确认的策略版本。')
                if (_timestamp(row['created_at']) > executed_at
                        or _timestamp(document['confirmation']['at']) > executed_at):
                    raise ValueError('成交后创建或确认的策略版本不能作为当时依据。')
                strategy.update(strategy_id=request['strategy_id'], version=version)
            document = {'trade_id': trade_id, 'version': latest + 1,
                        'previous_version': latest or None, 'recorded_at': now(),
                        'request': request, 'report': report, 'strategy': strategy,
                        'fill_sha256': _fingerprint(fill),
                        'knowledge_at_trade': 'not_established',
                        'usage': 'personal_review_only'}
            db.execute('INSERT INTO actual_fill_contexts VALUES (?, ?, ?, ?)',
                       (trade_id, document['version'], document['recorded_at'],
                        json.dumps(document, ensure_ascii=False)))
        return document

    def get(self, trade_id, version=None):
        trade_id = _text(trade_id, 'trade_id')
        self.ledger.get_fill(trade_id)
        if version is not None and (type(version) is not int or version < 1):
            raise ValueError('关联版本无效。')
        with self.store.connect() as db:
            row = db.execute('''SELECT document FROM actual_fill_contexts
                WHERE trade_id=? AND (? IS NULL OR version=?)
                ORDER BY version DESC LIMIT 1''', (trade_id, version, version)).fetchone()
        if row is None and version is not None:
            raise ValueError('未找到指定成交关联版本。')
        return json.loads(row[0]) if row else None

    def history(self, trade_id, *, limit=100, offset=0):
        trade_id = _text(trade_id, 'trade_id')
        self.ledger.get_fill(trade_id)
        if (type(limit) is not int or not 1 <= limit <= 1000
                or type(offset) is not int or offset < 0):
            raise ValueError('关联查询分页参数无效。')
        with self.store.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM actual_fill_contexts WHERE trade_id=?',
                               (trade_id,)).fetchone()[0]
            rows = db.execute('''SELECT document FROM actual_fill_contexts
                WHERE trade_id=? ORDER BY version DESC LIMIT ? OFFSET ?''',
                (trade_id, limit, offset)).fetchall()
        return {'total': total, 'items': [json.loads(row[0]) for row in rows]}
