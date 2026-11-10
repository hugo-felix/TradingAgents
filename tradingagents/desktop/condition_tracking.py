"""Explicit offline plans over immutable checks; no scheduler or notifications."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from tradingagents.desktop.store import now
from tradingagents.desktop.strategy_checks import StrategyCheckRegistry, _digest, _json, _timestamp

ENGINE = 'stockseek-condition-tracking/1'
CONTEXT = ('symbol', 'market', 'timeframe', 'price_basis', 'source')


def _condition(check, group, index):
    for condition in check['conditions']:
        if condition['group'] == group and condition['index'] == index:
            return condition
    raise ValueError('未找到指定条件。')


def _advance(plan, check, previous):
    condition = _condition(check, plan['group'], plan['index'])
    count = previous['consecutive'] if previous else 0
    watermark = previous['watermark'] if previous else {}
    result = {'consecutive': 0, 'watermark': watermark, 'hit': False}
    if _timestamp(check['as_of']) >= _timestamp(plan['expires_at']):
        return dict(result, status='expired', reason='计划已到期。')
    if condition['status'] not in {'met', 'not_met'}:
        return dict(result, status='paused', reason=condition['status'])
    observed = {}
    for side in ('left', 'right'):
        detail = condition[side]
        if not detail['observations']:
            continue
        if detail.get('freshness', {}).get('status') != 'within_limit':
            return dict(result, status='paused', reason='freshness_not_evaluated')
        observed[side] = max(_timestamp(row['observed_at'])
                             for row in detail['observations']).isoformat()
    if not observed:
        return dict(result, status='paused', reason='no_observation')
    if any(side in watermark and _timestamp(value) < _timestamp(watermark[side])
           for side, value in observed.items()):
        return dict(result, status='paused', reason='observation_regressed')
    # An explicit failure interrupts the run even when it revises the same observation.
    if condition['status'] == 'not_met':
        return dict(result, watermark=observed, status='not_met', reason='not_met')
    if any(side in watermark and _timestamp(value) <= _timestamp(watermark[side])
           for side, value in observed.items()):
        return dict(result, consecutive=count, status='waiting_for_observation',
                    reason='all_metric_operands_must_advance')
    total = min(count + 1, plan['required_consecutive'])
    hit = count < plan['required_consecutive'] <= total
    return dict(result, consecutive=total, watermark=observed, hit=hit,
                status='triggered' if hit else 'sustained' if total == count else 'observing',
                reason='distinct_observation')


class ConditionTrackingRegistry:
    def __init__(self, store):
        self.store = store
        self.checks = StrategyCheckRegistry(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS condition_tracking_plans (
                plan_id TEXT PRIMARY KEY, document TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS condition_tracking_events (
                event_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, check_id TEXT NOT NULL,
                document TEXT NOT NULL, UNIQUE(plan_id, check_id))''')

    def create(self, check_id, group, index, required_consecutive, expires_at, created_by):
        if (not isinstance(group, str) or type(index) is not int or index < 0
                or type(required_consecutive) is not int or not 1 <= required_consecutive <= 100):
            raise ValueError('请指定条件组、非负整数序号和 1–100 次连续观测。')
        if (not isinstance(created_by, str) or not created_by.strip()
                or len(created_by) > 200):
            raise ValueError('请提供不超过 200 字的具名计划创建人。')
        check = self.checks.get(check_id)
        _condition(check, group, index)
        if _timestamp(expires_at) <= _timestamp(check['as_of']):
            raise ValueError('计划到期时间须晚于起始检查时间。')
        identity = {'engine': ENGINE, 'seed_check_id': check_id, 'seed_sha256': _digest(check),
                    'strategy_id': check['strategy_id'], 'version': check['version'],
                    'check_engine': check['engine'], 'group': group, 'index': index,
                    'context': {key: check['data'][key] for key in CONTEXT},
                    'units': {name: series['unit'] for name, series in check['data']['series'].items()},
                    'required_consecutive': required_consecutive,
                    'starts_at': check['as_of'], 'expires_at': _timestamp(expires_at).isoformat(),
                    'created_by': created_by.strip()}
        plan_id = _digest(identity)
        document = dict(identity, plan_id=plan_id, created_at=now(),
                        limitations=[
                            '仅跟踪指定单条条件，不代表整体策略通过、交易建议或订单。',
                            '调用者主动提交检查；没有自动巡检、行情接入或消息发送。',
                            '时间、来源及数值由调用者声明，未核验供应商档案。',
                            '连续指提交序列中的独立观测，不证明交易日或观测完整覆盖。',
                            '指标必须配置最大年龄；未知、缺失或过期会清零连续计数。'])
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO condition_tracking_plans VALUES (?, ?)',
                       (plan_id, _json(document)))
        return self.get(plan_id)

    def get(self, plan_id):
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM condition_tracking_plans WHERE plan_id=?',
                             (plan_id,)).fetchone()
        if row is None:
            raise ValueError('未找到条件跟踪计划。')
        return json.loads(row[0])

    def evaluate(self, plan_id, check_id):
        plan = self.get(plan_id)
        check = self.checks.get(check_id)
        if (check['strategy_id'] != plan['strategy_id'] or check['version'] != plan['version']
                or check['engine'] != plan['check_engine']
                or any(check['data'][key] != plan['context'][key] for key in CONTEXT)
                or any(name in check['data']['series'] and check['data']['series'][name]['unit'] != unit
                       for name, unit in plan['units'].items())):
            raise ValueError('检查必须匹配计划的策略版本、引擎及数据上下文。')
        if _timestamp(check['as_of']) < _timestamp(plan['starts_at']):
            raise ValueError('检查不得早于计划起始时间。')
        with self.store.connect() as db:
            # Serialize read/advance/append so concurrent calls cannot lose a transition.
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT document FROM condition_tracking_events '
                                  'WHERE plan_id=? AND check_id=?', (plan_id, check_id)).fetchone()
            if existing:
                return json.loads(existing[0])
            row = db.execute('SELECT document FROM condition_tracking_events '
                             'WHERE plan_id=? ORDER BY rowid DESC LIMIT 1', (plan_id,)).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous and _timestamp(check['as_of']) <= _timestamp(previous['check']['as_of']):
                raise ValueError('新检查时间必须严格晚于最近一次记录；同一检查可幂等重试。')
            result = _advance(plan, check, previous['result'] if previous else None)
            identity = {'engine': ENGINE, 'plan_id': plan_id, 'check_id': check_id,
                        'check_sha256': _digest(check),
                        'previous_event_id': previous['event_id'] if previous else None}
            event_id = _digest(identity)
            event = dict(identity, event_id=event_id, created_at=now(), check=check, result=result)
            db.execute('INSERT INTO condition_tracking_events VALUES (?, ?, ?, ?)',
                       (event_id, plan_id, check_id, _json(event)))
        return event

    def history(self, plan_id):
        self.get(plan_id)
        with self.store.connect() as db:
            rows = db.execute('SELECT document FROM condition_tracking_events '
                              'WHERE plan_id=? ORDER BY rowid DESC', (plan_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list(self):
        with self.store.connect() as db:
            rows = db.execute('SELECT document FROM condition_tracking_plans '
                              'ORDER BY rowid DESC').fetchall()
        return [json.loads(row[0]) for row in rows]


def main(argv=None):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock
    from tradingagents.desktop.store import DesktopStore

    parser = argparse.ArgumentParser(description='StockSeek 离线条件跟踪，不发送通知')
    parser.add_argument('--workspace', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('create')
    create.add_argument('check_id')
    create.add_argument('group')
    create.add_argument('index', type=int)
    create.add_argument('--required-consecutive', type=int, required=True)
    create.add_argument('--expires-at', required=True)
    create.add_argument('--created-by', required=True)
    sub.add_parser('list')
    for action in ('get', 'history', 'evaluate'):
        command = sub.add_parser(action)
        command.add_argument('plan_id')
        if action == 'evaluate':
            command.add_argument('check_id')
    args = parser.parse_args(argv)
    try:
        if not (args.workspace / 'workspace.sqlite3').is_file():
            raise ValueError('工作空间数据库不存在。')
        if (args.workspace / INCOMPLETE).exists():
            raise ValueError('不能打开未完整恢复的工作空间。')
        with WorkspaceLock(args.workspace):
            registry = ConditionTrackingRegistry(DesktopStore(args.workspace))
            if args.action == 'create':
                result = registry.create(args.check_id, args.group, args.index,
                                         args.required_consecutive, args.expires_at, args.created_by)
            elif args.action == 'evaluate':
                result = registry.evaluate(args.plan_id, args.check_id)
            elif args.action == 'get':
                result = registry.get(args.plan_id)
            elif args.action == 'list':
                result = registry.list()
            else:
                result = registry.history(args.plan_id)
    except (OSError, ValueError, OverflowError, sqlite3.Error) as exc:
        parser.exit(1, f'操作失败：{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
