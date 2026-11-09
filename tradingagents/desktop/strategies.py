"""Append-only strategy definitions; confirmation never implies implementation or validation."""

from __future__ import annotations

import json
from uuid import uuid4

from tradingagents.desktop.store import now

RULE_FIELDS = ('entry', 'filters', 'exit', 'invalidation', 'position', 'portfolio', 'exceptions')
SCOPE_FIELDS = ('name', 'source', 'market', 'universe', 'timeframe', 'holding_period',
                'environment', 'observation_time', 'confirmation_time', 'execution_time',
                'execution_price')


def validate_definition(value):
    if not isinstance(value, dict):
        raise ValueError('策略定义必须是对象。')
    # JSON roundtrip isolates caller-owned objects and rejects non-finite parameters.
    try:
        result = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError('策略定义必须是有效 JSON。') from exc
    if not isinstance(result.get('name'), str) or not result['name'].strip():
        raise ValueError('请填写策略名称。')
    for field in SCOPE_FIELDS:
        if field in result and (not isinstance(result[field], str) or not result[field].strip()):
            raise ValueError(f'策略字段无效：{field}')
    rules = result.get('rules', {})
    if not isinstance(rules, dict) or set(rules) - set(RULE_FIELDS):
        raise ValueError('策略规则分类无效。')
    for rows in rules.values():
        if not isinstance(rows, list):
            raise ValueError('策略规则必须是列表。')
        for row in rows:
            if not isinstance(row, dict) or row.get('kind') not in {
                'author', 'model_draft', 'personal', 'quantitative', 'market_adaptation',
            } or row.get('state') not in {'pending', 'manual', 'defined'}:
                raise ValueError('规则必须标注来源类型及定义状态。')
            if not all(isinstance(row.get(k), str) and row[k].strip()
                       for k in ('text', 'citation')):
                raise ValueError('规则必须保留内容和出处或假设说明。')
    return result


class StrategyRegistry:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS strategy_versions (
                strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
                created_at TEXT NOT NULL, document TEXT NOT NULL,
                PRIMARY KEY(strategy_id, version))''')

    def save(self, definition, *, strategy_id=None, expected_version=0):
        definition = validate_definition(definition)
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError('策略版本无效。')
        strategy_id = strategy_id or uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = db.execute('SELECT MAX(version) FROM strategy_versions WHERE strategy_id=?',
                                (strategy_id,)).fetchone()[0] or 0
            if latest != expected_version:
                raise ValueError('策略已更新，请读取最新版本后重试。')
            document = {'strategy_id': strategy_id, 'version': latest + 1,
                        'created_at': now(), 'definition': definition, 'status': 'draft',
                        'confirmation': None, 'implementation': 'not_implemented',
                        'backtest': 'not_evaluated', 'out_of_sample': 'not_evaluated'}
            self._insert(db, document)
        return document

    @staticmethod
    def _insert(db, document):
        db.execute('INSERT INTO strategy_versions VALUES (?, ?, ?, ?)', (
            document['strategy_id'], document['version'], document['created_at'],
            json.dumps(document, ensure_ascii=False, allow_nan=False)))

    def get(self, strategy_id, version=None):
        with self.store.connect() as db:
            row = db.execute('''SELECT document FROM strategy_versions WHERE strategy_id=?
                AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1''',
                             (strategy_id, version, version)).fetchone()
        if not row:
            raise ValueError('未找到策略版本。')
        return json.loads(row[0])

    def list_strategies(self):
        with self.store.connect() as db:
            rows = db.execute('''SELECT s.document FROM strategy_versions s
                JOIN (SELECT strategy_id, MAX(version) AS version FROM strategy_versions
                      GROUP BY strategy_id) latest
                USING (strategy_id, version)
                ORDER BY s.created_at DESC, s.strategy_id''').fetchall()
        return [json.loads(row[0]) for row in rows]

    def history(self, strategy_id):
        with self.store.connect() as db:
            rows = db.execute('''SELECT document FROM strategy_versions WHERE strategy_id=?
                ORDER BY version DESC''', (strategy_id,)).fetchall()
        if not rows:
            raise ValueError('未找到策略版本。')
        return [json.loads(row[0]) for row in rows]

    def confirm(self, strategy_id, *, expected_version, confirmed_by):
        if not isinstance(confirmed_by, str) or not confirmed_by.strip():
            raise ValueError('请记录确认人。')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT document FROM strategy_versions WHERE strategy_id=?
                ORDER BY version DESC LIMIT 1''', (strategy_id,)).fetchone()
            if not row:
                raise ValueError('未找到策略版本。')
            document = json.loads(row[0])
            if type(expected_version) is not int or document['version'] != expected_version:
                raise ValueError('策略已更新，请读取最新版本后重试。')
            definition = document['definition']
            if any(not definition.get(k) for k in SCOPE_FIELDS) or any(
                not definition.get('rules', {}).get(k) for k in RULE_FIELDS
            ):
                raise ValueError('策略范围、交易时点及规则尚未完整。')
            if any(r['state'] == 'pending' for rows in definition['rules'].values() for r in rows):
                raise ValueError('仍有待定义规则，不能确认。')
            document.update(version=expected_version + 1, created_at=now(), status='confirmed',
                            confirmation={'by': confirmed_by.strip(), 'at': now(),
                                          'draft_version': expected_version})
            self._insert(db, document)
        return document
