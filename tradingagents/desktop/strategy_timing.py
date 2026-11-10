"""Immutable retrospective timing audits, never fills or a trading calendar provider."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from tradingagents.desktop.store import now
from tradingagents.desktop.strategies import validate_timing
from tradingagents.desktop.strategy_checks import (
    StrategyCheckRegistry,
    _digest,
    _json,
    _number,
    _text,
    _timestamp,
)

ENGINE = 'stockseek-timing/1'
MAX_BYTES = 8 * 1024 * 1024
MAX_SESSIONS = 10000


def validate_evidence(value, market):
    """Validate structure and chronology, not caller claims of calendar completeness."""
    if not isinstance(value, dict):
        raise ValueError('时序证据必须是对象。')
    try:
        serialized = _json(value)
        if len(serialized.encode('utf-8')) > MAX_BYTES:
            raise ValueError('时序证据超过 8 MiB。')
        value = json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise ValueError('时序证据必须是 8 MiB 以内的有限值 JSON。') from exc
    if set(value) != {'calendar', 'execution_price'}:
        raise ValueError('时序证据需且仅需 calendar 和 execution_price（未知用 null）。')
    calendar = value['calendar']
    fields = {'market', 'source', 'revision', 'coverage_start', 'coverage_end',
              'complete', 'sessions'}
    if (not isinstance(calendar, dict) or set(calendar) != fields
            or calendar['market'] != market
            or not all(_text(calendar[k]) for k in ('source', 'revision'))
            or type(calendar['complete']) is not bool
            or not isinstance(calendar['sessions'], list)
            or len(calendar['sessions']) > MAX_SESSIONS):
        raise ValueError('日历需明确同一市场、来源、版本、覆盖区间、完整性和时段列表。')
    start, end = (_timestamp(calendar[k]) for k in ('coverage_start', 'coverage_end'))
    if start >= end:
        raise ValueError('日历覆盖区间必须递增。')
    intervals = []
    for session in calendar['sessions']:
        if not isinstance(session, dict) or set(session) != {'open_at', 'close_at'}:
            raise ValueError('每个交易时段需且仅需开盘和收盘时间。')
        opened, closed = (_timestamp(session[k]) for k in ('open_at', 'close_at'))
        if not start <= opened < closed <= end:
            raise ValueError('交易时段必须递增且完整落在日历覆盖区间内。')
        intervals.append((opened, closed))
    intervals.sort()
    if any(current[0] <= previous[1] for previous, current in zip(
            intervals, intervals[1:], strict=False)):
        raise ValueError('交易时段不能重复、重叠或共享边界。')
    price = value['execution_price']
    if price is not None:
        fields = {'at', 'available_at', 'value', 'unit', 'price_basis', 'source', 'revision'}
        if (not isinstance(price, dict) or set(price) != fields
                or not all(_text(price[k]) for k in ('unit', 'price_basis', 'source', 'revision'))):
            raise ValueError('执行价格需明确时间、可得时间、正数价格、单位、口径和来源版本。')
        if (_number(price['value']) <= 0
                or _timestamp(price['available_at']) < _timestamp(price['at'])):
            raise ValueError('执行价格须为正数，可得时间不能早于价格时间。')
    return value


def _audit(check, contract, evidence):
    cutoff = _timestamp(check['as_of'])
    observations = [row for condition in check['conditions'] for side in ('left', 'right')
                    for row in condition.get(side, {}).get('observations', [])]
    reasons = []
    invalid = False
    observation_at = max((_timestamp(row['observed_at']) for row in observations), default=None)
    latest_available = max((_timestamp(row['available_at']) for row in observations), default=None)
    if observation_at is None:
        reasons.append('没有实际参与计算的观测，不能确认完成观测的时间。')
    elif cutoff - latest_available < timedelta(seconds=contract['confirmation_delay_seconds']):
        invalid = True
        reasons.append('确认时间早于最后采用数据可得时间加上约定确认延迟。')
    if check['data']['price_basis'] != contract['price_basis']:
        invalid = True
        reasons.append('条件数据的价格口径与已确认时序契约不一致。')
    calendar = evidence['calendar']
    start, end = (_timestamp(calendar[k]) for k in ('coverage_start', 'coverage_end'))
    expected = None
    if not calendar['complete'] or not start <= cutoff < end:
        reasons.append('日历未声明完整覆盖确认时间，不能确定下一开盘。')
    else:
        later = [row for row in calendar['sessions'] if _timestamp(row['open_at']) > cutoff]
        if later:
            expected = min(later, key=lambda row: _timestamp(row['open_at']))['open_at']
        else:
            reasons.append('日历覆盖区间内没有确认时间之后的开盘，需扩展证据。')
    price = evidence['execution_price']
    if price is None:
        reasons.append('未提供执行价格，不能核对执行价格时点和口径。')
    else:
        if price['price_basis'] != contract['price_basis'] or price['unit'] != contract['price_unit']:
            invalid = True
            reasons.append('执行价格口径或单位与已确认时序契约不一致。')
        if _timestamp(price['at']) <= cutoff:
            invalid = True
            reasons.append('执行价格时间必须严格晚于确认时间，不能回填当日收盘或同刻开盘。')
        if expected and _timestamp(price['at']) != _timestamp(expected):
            invalid = True
            reasons.append('执行价格不属于确认之后的下一交易时段开盘。')
    return {'status': 'inconsistent' if invalid else 'insufficient_data' if reasons else 'consistent',
            'reasons': reasons, 'observation_at': observation_at.isoformat() if observation_at else None,
            'latest_input_available_at': latest_available.isoformat() if latest_available else None,
            'confirmed_at': check['as_of'], 'expected_execution_at': expected}


class StrategyTimingRegistry:
    def __init__(self, store):
        self.store = store
        self.checks = StrategyCheckRegistry(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS strategy_timing_audits (
                audit_id TEXT PRIMARY KEY, check_id TEXT NOT NULL, document TEXT NOT NULL)''')

    def assess(self, check_id, evidence):
        check = self.checks.get(check_id)
        strategy = self.checks.strategies.get(check['strategy_id'], check['version'])
        contract = validate_timing(strategy['definition'].get('timing'))
        evidence = validate_evidence(evidence, check['data']['market'])
        identity = {'engine': ENGINE, 'check_id': check_id, 'check_sha256': _digest(check),
                    'evidence_sha256': _digest(evidence)}
        audit_id = _digest(identity)
        document = dict(identity, audit_id=audit_id, created_at=now(),
                        strategy_id=check['strategy_id'], version=check['version'],
                        contract=contract, evidence=evidence, result=_audit(check, contract, evidence),
                        condition_counts=check['counts'], evidence_status='caller_supplied_unverified',
                        limitations=[
                            '仅回顾核对时序，不是信号通过、可成交证明、订单、模拟成交或回测。',
                            '完成观测、日历完整性、价格和可得时间均由调用者声明，未核验供应商档案。',
                            '确认时间取条件检查 as_of；下一开盘为完整覆盖区间内严格晚于确认的首个开盘。',
                            '执行价格仅用于事后核对，不作为确认时已知的条件输入或价格预测。',
                            '未核对停牌、涨跌停、流动性、费用、滑点、数量与账户约束。',
                            '人工、缺失、未实现与不满足的条件保持原状态；一致不表示整体策略满足。'])
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO strategy_timing_audits VALUES (?, ?, ?)',
                       (audit_id, check_id, _json(document)))
        return self.get(audit_id)

    def get(self, audit_id):
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM strategy_timing_audits WHERE audit_id=?',
                             (audit_id,)).fetchone()
        if row is None:
            raise ValueError('未找到时序核对快照。')
        return json.loads(row[0])

    def history(self, check_id):
        self.checks.get(check_id)
        with self.store.connect() as db:
            rows = db.execute('SELECT document FROM strategy_timing_audits '
                              'WHERE check_id=? ORDER BY rowid DESC', (check_id,)).fetchall()
        return [{key: value for key, value in json.loads(row[0]).items() if key != 'evidence'}
                for row in rows]


def main(argv=None):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock
    from tradingagents.desktop.store import DesktopStore

    parser = argparse.ArgumentParser(description='StockSeek 离线时序核对，不生成成交')
    parser.add_argument('--workspace', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    assess = sub.add_parser('assess')
    assess.add_argument('check_id')
    assess.add_argument('evidence', type=Path)
    get = sub.add_parser('get')
    get.add_argument('audit_id')
    history = sub.add_parser('history')
    history.add_argument('check_id')
    args = parser.parse_args(argv)
    try:
        if not (args.workspace / 'workspace.sqlite3').is_file():
            raise ValueError('工作空间数据库不存在。')
        if (args.workspace / INCOMPLETE).exists():
            raise ValueError('不能打开未完整恢复的工作空间。')
        with WorkspaceLock(args.workspace):
            registry = StrategyTimingRegistry(DesktopStore(args.workspace))
            if args.action == 'assess':
                with args.evidence.open('rb') as source:
                    content = source.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ValueError('时序证据超过 8 MiB。')
                result = registry.assess(args.check_id, json.loads(content.decode('utf-8-sig')))
            elif args.action == 'get':
                result = registry.get(args.audit_id)
            else:
                result = registry.history(args.check_id)
    except (OSError, ValueError, OverflowError, sqlite3.Error) as exc:
        parser.exit(1, f'操作失败：{exc}\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
