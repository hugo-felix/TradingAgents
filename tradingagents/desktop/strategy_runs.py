"""Append-only, externally supplied run evidence; never a validation certificate."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime

from tradingagents.desktop.store import now
from tradingagents.desktop.strategies import StrategyRegistry


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def validate_run(value):
    """Validate the manifest, without claiming its supplied evidence is true."""
    if not isinstance(value, dict):
        raise ValueError('运行记录必须是对象。')
    try:
        value = json.loads(_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError('运行记录必须是有限值 JSON。') from exc
    if (not isinstance(value.get('run_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value['run_id'])):
        raise ValueError('请提供稳定的运行标识（字母、数字、点、下划线或连字符）。')
    if value.get('stage') not in ('backtest', 'out_of_sample'):
        raise ValueError('请区分回测和样本外运行。')
    if value.get('outcome') not in ('completed', 'failed', 'cancelled', 'inconclusive'):
        raise ValueError('运行结果必须区分完成、失败、取消和不确定。')
    for field in ('recorded_by', 'summary'):
        if not _text(value.get(field)):
            raise ValueError(f'请填写运行记录字段：{field}')
    for field in ('assumptions', 'limitations'):
        if (not isinstance(value.get(field), list) or not value[field]
                or not all(_text(item) for item in value[field])):
            raise ValueError(f'请显式记录运行假设与限制：{field}')
    implementation = value.get('implementation')
    if not isinstance(implementation, dict) or not all(
            _text(implementation.get(k)) for k in ('entrypoint', 'revision')):
        raise ValueError('请记录实现入口和代码版本。')
    parameters = value.get('parameters')
    if not isinstance(parameters, dict) or any(
            not _text(key) or not isinstance(item, dict) or 'value' not in item
            or not _text(item.get('source')) for key, item in parameters.items()):
        raise ValueError('每个参数必须记录值及出处或假设；无参数使用空对象。')
    data = value.get('data')
    if not isinstance(data, list) or not data:
        raise ValueError('请记录使用的数据快照。')
    for item in data:
        if (not isinstance(item, dict)
                or not all(_text(item.get(k)) for k in ('source', 'version', 'sha256'))
                or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
            raise ValueError('数据必须记录来源、版本及 SHA-256 指纹。')
    period = value.get('period')
    try:
        if not isinstance(period, dict):
            raise ValueError
        start, end = (date.fromisoformat(period[k]) for k in ('start', 'end'))
        if start > end:
            raise ValueError
        started, finished = (datetime.fromisoformat(value[k])
                             for k in ('started_at', 'finished_at'))
        if (started.utcoffset() is None or finished.utcoffset() is None
                or started > finished):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('运行时间须含时区，样本区间和运行起止顺序必须有效。') from exc
    duration = value.get('duration_seconds')
    if (type(duration) not in (int, float) or duration < 0
            or (type(duration) is float and not math.isfinite(duration))):
        raise ValueError('请记录非负运行耗时（秒）。')
    metrics = value.get('metrics')
    if not isinstance(metrics, dict) or any(
            not _text(key) or not isinstance(item, dict) or not _text(item.get('unit'))
            or 'value' not in item or (item['value'] is not None
                and type(item['value']) not in (int, float)) for key, item in metrics.items()):
        raise ValueError('指标必须记录数值与单位；缺失值使用 null，无指标使用空对象。')
    return value


class StrategyRunRegistry:
    def __init__(self, store):
        self.store = store
        self.strategies = StrategyRegistry(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS strategy_runs (
                strategy_id TEXT NOT NULL, version INTEGER NOT NULL, run_id TEXT NOT NULL,
                document TEXT NOT NULL, PRIMARY KEY(strategy_id, version, run_id),
                FOREIGN KEY(strategy_id, version) REFERENCES strategy_versions(strategy_id, version)
            )''')

    def _strategy(self, strategy_id, version):
        if type(version) is not int or version < 1:
            raise ValueError('运行证据必须绑定明确的策略版本。')
        return self.strategies.get(strategy_id, version)

    def record(self, strategy_id, version, manifest):
        manifest = validate_run(manifest)
        strategy = self._strategy(strategy_id, version)
        if strategy['status'] != 'confirmed':
            raise ValueError('运行证据只能绑定已确认的策略版本。')
        document = {
            'schema_version': 1, 'strategy_id': strategy_id, 'version': version,
            'definition_sha256': hashlib.sha256(
                _json(strategy['definition']).encode('utf-8')).hexdigest(),
            'recorded_at': now(), 'evidence_status': 'external_unverified',
            'manifest': manifest,
        }
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT document FROM strategy_runs
                WHERE strategy_id=? AND version=? AND run_id=?''',
                             (strategy_id, version, manifest['run_id'])).fetchone()
            if row:
                previous = json.loads(row[0])
                if _json(previous['manifest']) != _json(manifest):
                    raise ValueError('该运行标识已登记不同内容；请核对或使用新的运行标识。')
                return previous
            db.execute('INSERT INTO strategy_runs VALUES (?, ?, ?, ?)',
                       (strategy_id, version, manifest['run_id'], _json(document)))
        return document

    def list_runs(self, strategy_id, version):
        self._strategy(strategy_id, version)
        with self.store.connect() as db:
            rows = db.execute('''SELECT document FROM strategy_runs
                WHERE strategy_id=? AND version=? ORDER BY rowid DESC''',
                              (strategy_id, version)).fetchall()
        return [json.loads(row[0]) for row in rows]
