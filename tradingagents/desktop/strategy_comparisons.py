"""Immutable, explicitly personal comparisons of two independent condition checks."""

from __future__ import annotations

import json

from tradingagents.desktop.store import now
from tradingagents.desktop.strategy_checks import (
    StrategyCheckRegistry,
    _digest,
    _json,
    _timestamp,
)

ENGINE = 'stockseek-comparisons/1'
VERDICTS = {'not_assessed', 'conflict_identified', 'no_conflict_identified'}


def differences(left, right):
    """Describe context differences without inferring rule equivalence or conflict."""
    fields = ('symbol', 'market', 'timeframe', 'price_basis', 'source', 'revision')
    result = [field for field in fields if left['data'][field] != right['data'][field]]
    if _timestamp(left['as_of']) != _timestamp(right['as_of']):
        result.append('as_of')
    if _digest(left['data']['series']) != _digest(right['data']['series']):
        result.append('series')
    return result


class StrategyComparisonRegistry:
    def __init__(self, store):
        self.store = store
        self.checks = StrategyCheckRegistry(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS strategy_comparisons (
                comparison_id TEXT PRIMARY KEY, left_id TEXT NOT NULL,
                right_id TEXT NOT NULL, document TEXT NOT NULL)''')

    def preview(self, left_id, right_id):
        if not all(isinstance(value, str) and value.strip()
                   for value in (left_id, right_id)):
            raise ValueError('请提供两个检查标识。')
        if left_id == right_id:
            raise ValueError('请选择两个不同的条件检查快照。')
        entries = []
        for check_id in (left_id, right_id):
            check = self.checks.get(check_id)
            strategy = self.checks.strategies.get(check['strategy_id'], check['version'])
            entries.append({'strategy': strategy, 'check': check})
        return {'entries': entries,
                'context_differences': differences(entries[0]['check'], entries[1]['check'])}

    def save(self, left_id, right_id, verdict, note, assessed_by):
        if not isinstance(assessed_by, str) or not assessed_by.strip():
            raise ValueError('请提供具名判断人。')
        if not isinstance(verdict, str) or verdict not in VERDICTS:
            raise ValueError('请选择明确的人工冲突判断状态。')
        if not isinstance(note, str) or len(note) > 10000 or len(assessed_by) > 200:
            raise ValueError('说明上限为 10000 字，判断人上限为 200 字。')
        if verdict != 'not_assessed' and not note.strip():
            raise ValueError('已判断时须说明分歧或未识别分歧的依据及适用条件。')
        preview = self.preview(left_id, right_id)
        entries = preview['entries']
        identity = {'engine': ENGINE, 'check_ids': [left_id, right_id],
                    'entry_sha256': [_digest(entry) for entry in entries],
                    'verdict': verdict, 'note': note.strip(), 'assessed_by': assessed_by.strip()}
        comparison_id = _digest(identity)
        document = dict(identity, comparison_id=comparison_id, created_at=now(), entries=entries,
                        context_differences=preview['context_differences'],
                        limitations=[
                            '冲突判断为具名个人意见，不是模型判断或自动规则等价检验。',
                            '各体系条件独立保留；不平均评分、不合成信号、不决定买卖或执行优先级。',
                            '上下文相同不代表策略可比；不同周期、规则含义和适用环境仍需人工核对。',
                            '输入来源及可得时间由调用者声明，未独立核验；条件满足不表示策略有效。'])
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO strategy_comparisons VALUES (?, ?, ?, ?)',
                       (comparison_id, left_id, right_id, _json(document)))
        return self.get(comparison_id)

    def get(self, comparison_id):
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM strategy_comparisons WHERE comparison_id=?',
                             (comparison_id,)).fetchone()
        if row is None:
            raise ValueError('未找到策略对照快照。')
        return json.loads(row[0])

    def history(self, check_id):
        self.checks.get(check_id)
        with self.store.connect() as db:
            rows = db.execute('SELECT document FROM strategy_comparisons '
                              'WHERE left_id=? OR right_id=? ORDER BY rowid DESC',
                              (check_id, check_id)).fetchall()
        return [{key: value for key, value in json.loads(row[0]).items() if key != 'entries'}
                for row in rows]
