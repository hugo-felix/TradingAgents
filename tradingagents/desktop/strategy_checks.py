"""Offline, version-bound condition checks; never orders or strategy validation.

Only explicit machine expressions are evaluated. Prose, manual conditions and
unsupported expressions cannot turn into a passed trading condition.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction

from tradingagents.desktop.store import now
from tradingagents.desktop.strategies import RULE_FIELDS, StrategyRegistry

ENGINE = 'stockseek-conditions/2'
MAX_OBSERVATIONS = 100000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('时间必须是含时区的 ISO 时间。')
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
    except ValueError as exc:
        raise ValueError('时间必须是含时区的 ISO 时间。') from exc
    return parsed


def _number(value):
    if type(value) not in (int, float) or not Decimal(str(value)).is_finite():
        raise ValueError('数值必须有限，缺失值请用 null；不接受布尔值。')
    return Decimal(str(value))


def validate_data(value):
    """Snapshot supplied values, not their truth or the supplier's availability claims."""
    if not isinstance(value, dict):
        raise ValueError('检查数据必须是对象。')
    try:
        value = json.loads(_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError('检查数据必须是有限值 JSON。') from exc
    fields = {'symbol', 'market', 'timeframe', 'price_basis', 'source', 'revision', 'series'}
    if set(value) != fields or not all(_text(value.get(k)) for k in fields - {'series'}):
        raise ValueError('数据必须明确证券、市场、周期、价格口径、来源和版本。')
    if not isinstance(value['series'], dict) or not value['series']:
        raise ValueError('请提供有名称、单位与观测记录的序列。')
    count = 0
    for name, series in value['series'].items():
        if (not _text(name) or not isinstance(series, dict)
                or set(series) != {'unit', 'observations'} or not _text(series['unit'])
                or not isinstance(series['observations'], list)):
            raise ValueError('序列必须明确名称、单位及观测列表。')
        seen = set()
        count += len(series['observations'])
        if count > MAX_OBSERVATIONS:
            raise ValueError('检查数据超过 100000 条观测上限。')
        for row in series['observations']:
            if not isinstance(row, dict) or set(row) != {'observed_at', 'available_at', 'value'}:
                raise ValueError('每条观测必须有观测时间、可得时间及数值或 null。')
            observed, available = (_timestamp(row[k]) for k in ('observed_at', 'available_at'))
            if available < observed:
                raise ValueError('观测可得时间不得早于观测时间。')
            identity = (observed, available)
            if identity in seen:
                raise ValueError('同一观测与可得时间不能有重复或冲突记录。')
            seen.add(identity)
            if row['value'] is not None:
                _number(row['value'])
    return value


class _Unavailable(ValueError):
    pass


class _Unsupported(ValueError):
    pass


def _operand(expression, data, cutoff):
    if not isinstance(expression, dict):
        raise ValueError('数值表达式必须是对象。')
    if 'constant' in expression:
        if (set(expression) != {'constant', 'unit', 'source'}
                or not _text(expression['unit']) or not _text(expression['source'])):
            raise ValueError('常量必须明确数值、单位及出处或假设。')
        value = _number(expression['constant'])
        return value, {'value': str(value), 'unit': expression['unit'],
                       'source': expression['source'], 'observations': []}
    if 'metric' not in expression:
        raise _Unsupported('未实现的数值表达式。')
    max_age = expression.get('max_age_seconds')
    if 'max_age_seconds' in expression and (
            type(max_age) is not int or not 0 <= max_age <= 31536000):
        raise ValueError('最大观测年龄须为 0–31536000 的整数秒；不推断交易日历。')
    fields = set(expression) - {'max_age_seconds'}
    mean = 'aggregate' in expression or 'window' in expression
    if mean:
        if (fields != {'metric', 'aggregate', 'window'}
                or expression['aggregate'] != 'mean'
                or type(expression['window']) is not int or not 1 <= expression['window'] <= 10000):
            raise ValueError('均值需明确 aggregate=mean 和 1–10000 条观测窗口。')
    elif fields != {'metric'}:
        raise ValueError('最新值表达式只接受 metric 和可选 max_age_seconds。')
    if not _text(expression['metric']):
        raise ValueError('指标名无效。')
    series = data['series'].get(expression['metric'])
    if series is None:
        raise _Unavailable('数据中没有所需指标。')
    # For each observed instant, choose the latest revision available at cutoff.
    # A newly missing revision remains missing; never resurrect an earlier value.
    revisions = {}
    for row in series['observations']:
        observed, available = (_timestamp(row[k]) for k in ('observed_at', 'available_at'))
        if (observed <= cutoff and available <= cutoff
                and (observed not in revisions or available > revisions[observed][0])):
            revisions[observed] = (available, row)
    size = expression['window'] if mean else 1
    selected = [revisions[key][1] for key in sorted(revisions)[-size:]]
    if len(selected) < size or any(row['value'] is None for row in selected):
        raise _Unavailable('截止检查时刻的观测不足或含缺失值；未填补。')
    # Exact rational arithmetic avoids rounding a mean across a comparison boundary.
    value = (sum((Fraction(_number(row['value'])) for row in selected), Fraction(0)) / size
             if mean else _number(selected[0]['value']))
    age = cutoff - _timestamp(selected[-1]['observed_at'])
    freshness = {
        'status': ('not_evaluated' if max_age is None else
                   'stale' if age > timedelta(seconds=max_age) else 'within_limit'),
        'latest_observed_at': selected[-1]['observed_at'],
        'age_seconds': age.total_seconds(), 'max_age_seconds': max_age,
    }
    return value, {'value': str(value), 'unit': series['unit'],
                   'metric': expression['metric'], 'aggregate': 'mean' if mean else 'latest',
                   'window': size, 'observations': selected, 'freshness': freshness}


def _condition(row, data, cutoff):
    if row['state'] == 'manual':
        return {'status': 'manual', 'reason': '保留人工检查，不自动推断满足。'}
    if row['state'] != 'defined':
        return {'status': 'not_implemented', 'reason': '规则尚未定义。'}
    expression = row.get('check')
    if expression is None:
        return {'status': 'not_implemented', 'reason': '没有明确的机器表达式，不从文字生成。'}
    try:
        if not isinstance(expression, dict) or set(expression) != {'op', 'left', 'right'}:
            raise ValueError('比较表达式需且仅需 op、left、right。')
        if expression['op'] not in ('gt', 'ge', 'lt', 'le', 'eq'):
            raise _Unsupported('未实现的比较运算。')
        left, left_detail = _operand(expression['left'], data, cutoff)
        right, right_detail = _operand(expression['right'], data, cutoff)
        if left_detail['unit'] != right_detail['unit']:
            raise ValueError('比较单位不一致；未执行单位或币种转换。')
        if any(detail.get('freshness', {}).get('status') == 'stale'
               for detail in (left_detail, right_detail)):
            return {'status': 'stale_data', 'op': expression['op'],
                    'reason': '最新观测超过本规则的最大年龄；暂停比较，未推断交易日历。',
                    'left': left_detail, 'right': right_detail}
        matched = {'gt': left > right, 'ge': left >= right, 'lt': left < right,
                   'le': left <= right, 'eq': left == right}[expression['op']]
        return {'status': 'met' if matched else 'not_met', 'op': expression['op'],
                'left': left_detail, 'right': right_detail}
    except _Unavailable as exc:
        return {'status': 'insufficient_data', 'reason': str(exc)}
    except _Unsupported as exc:
        return {'status': 'not_implemented', 'reason': str(exc)}
    except ValueError as exc:
        return {'status': 'invalid_rule', 'reason': str(exc)}


class StrategyCheckRegistry:
    """Persist exact input and results without changing strategy validation status."""

    def __init__(self, store):
        self.store = store
        self.strategies = StrategyRegistry(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS strategy_condition_checks (
                check_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL,
                version INTEGER NOT NULL, document TEXT NOT NULL)''')

    def check(self, strategy_id, version, *, as_of, data):
        if type(version) is not int or version < 1:
            raise ValueError('条件检查必须指定确切策略版本。')
        strategy = self.strategies.get(strategy_id, version)
        if strategy['status'] != 'confirmed':
            raise ValueError('只能检查已人工确认的策略版本。')
        cutoff = _timestamp(as_of)
        data = validate_data(data)
        definition = strategy['definition']
        if any(data[key] != definition[key] for key in ('market', 'timeframe')):
            raise ValueError('数据市场或周期与策略版本不一致。')
        identity = {'engine': ENGINE, 'strategy_id': strategy_id, 'version': version,
                    'definition_sha256': _digest(definition), 'as_of': as_of,
                    'data_sha256': _digest(data)}
        check_id = _digest(identity)
        conditions = []
        for group in RULE_FIELDS:
            for index, rule in enumerate(definition['rules'][group]):
                conditions.append({'group': group, 'index': index, 'rule': rule,
                                   **_condition(rule, data, cutoff)})
        document = dict(identity, check_id=check_id, created_at=now(), data=data,
                        conditions=conditions,
                        counts=dict(Counter(row['status'] for row in conditions)),
                        evidence_status='caller_supplied_unverified',
                        limitations=[
                            '仅计算明确表达式；结果不是整体策略通过、交易建议或成交。',
                            '来源、可得时间及修订由调用者提供，未核验供应商原始档案。',
                            '仅校验市场和周期；股票池、环境、单位和价格口径需人工核对。',
                            '均值为最近 N 个观测时刻，不补缺，不证明连续交易日或完成周线。',
                            '过期仅按明确配置的最大观测年龄判断；未配置时新鲜度未评估。',
                            '年龄从最新观测时刻计算，修订不会重置；不证明窗口连续或已完成。',
                            '不计算交易日、执行价格、组合、费用、回测或样本外有效性。'])
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO strategy_condition_checks VALUES (?, ?, ?, ?)',
                       (check_id, strategy_id, version, _json(document)))
        return self.get(check_id)

    def get(self, check_id):
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM strategy_condition_checks WHERE check_id=?',
                             (check_id,)).fetchone()
        if row is None:
            raise ValueError('未找到条件检查快照。')
        return json.loads(row[0])

    def history(self, strategy_id, version):
        if type(version) is not int or version < 1:
            raise ValueError('请选择确切策略版本。')
        self.strategies.get(strategy_id, version)
        with self.store.connect() as db:
            rows = db.execute('SELECT document FROM strategy_condition_checks '
                              'WHERE strategy_id=? AND version=? ORDER BY rowid DESC',
                              (strategy_id, version)).fetchall()
        return [{key: value for key, value in json.loads(row[0]).items()
                 if key not in {'data', 'conditions'}} for row in rows]
