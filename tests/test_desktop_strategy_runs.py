import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import RULE_FIELDS, SCOPE_FIELDS, StrategyRegistry
from tradingagents.desktop.strategy_runs import StrategyRunRegistry


def manifest():
    return {
        'run_id': 'offline-001', 'stage': 'backtest', 'outcome': 'failed',
        'recorded_by': 'tester', 'summary': '合成运行失败，无投资结论',
        'assumptions': ['合成数据'], 'limitations': ['未运行真实策略'],
        'implementation': {'entrypoint': 'synthetic:run', 'revision': 'fixture-v1'},
        'parameters': {'lookback': {'value': 5, 'source': '测试假设'}},
        'data': [{'source': 'synthetic', 'version': 'fixture-v1', 'sha256': 'a' * 64}],
        'period': {'start': '2020-01-01', 'end': '2020-01-31'},
        'started_at': '2026-11-09T10:00:00+08:00',
        'finished_at': '2026-11-09T10:00:02+08:00', 'duration_seconds': 2,
        'metrics': {'return': {'value': None, 'unit': 'ratio'}},
    }


def setup_registry(tmp_path):
    store = DesktopStore(tmp_path)
    registry = StrategyRunRegistry(store)
    strategies = registry.strategies
    draft = strategies.save({**dict.fromkeys(SCOPE_FIELDS, '合成假设'), 'rules': {
        key: [{'text': '人工核对', 'citation': '测试', 'kind': 'personal', 'state': 'manual'}]
        for key in RULE_FIELDS}})
    confirmed = strategies.confirm(draft['strategy_id'], expected_version=1, confirmed_by='tester')
    return store, registry, confirmed


@pytest.mark.parametrize('outcome', ['completed', 'failed', 'cancelled', 'inconclusive'])
def test_historical_binding_survives_edits_and_restart_without_certification(tmp_path, outcome):
    store, registry, confirmed = setup_registry(tmp_path)
    sid = confirmed['strategy_id']
    latest = registry.strategies.save(confirmed['definition'], strategy_id=sid, expected_version=2)
    supplied = manifest()
    supplied['outcome'] = outcome
    record = registry.record(sid, 2, supplied)
    assert record['version'] == 2 and record['evidence_status'] == 'external_unverified'
    assert record['definition_sha256'] == hashlib.sha256(json.dumps(
        confirmed['definition'], ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode('utf-8')).hexdigest()
    supplied['summary'] = 'mutated by caller'
    record['manifest']['summary'] = 'mutated response'
    reopened = StrategyRunRegistry(DesktopStore(tmp_path))
    records = reopened.list_runs(sid, 2)
    assert records[0]['manifest']['summary'] == manifest()['summary']
    assert reopened.list_runs(sid, 3) == []
    assert StrategyRegistry(store).get(sid, 2) == confirmed
    assert StrategyRegistry(store).get(sid) == latest
    assert confirmed['backtest'] == confirmed['out_of_sample'] == 'not_evaluated'


def test_idempotency_conflict_and_independent_runs(tmp_path):
    _, registry, confirmed = setup_registry(tmp_path)
    sid = confirmed['strategy_id']
    original = registry.record(sid, 2, manifest())
    assert registry.record(sid, 2, manifest()) == original
    changed = manifest()
    changed['summary'] = 'different result'
    with pytest.raises(ValueError, match='不同内容'):
        registry.record(sid, 2, changed)
    changed['run_id'] = 'offline-002'
    changed['stage'] = 'out_of_sample'
    second = registry.record(sid, 2, changed)
    assert registry.list_runs(sid, 2) == [second, original]


def test_parameter_types_are_not_silently_treated_as_same_record(tmp_path):
    _, registry, confirmed = setup_registry(tmp_path)
    supplied = manifest()
    supplied['parameters']['lookback']['value'] = 1
    registry.record(confirmed['strategy_id'], 2, supplied)
    supplied['parameters']['lookback']['value'] = True
    with pytest.raises(ValueError, match='不同内容'):
        registry.record(confirmed['strategy_id'], 2, supplied)


def test_concurrent_duplicates_and_new_confirmed_version_are_isolated(tmp_path):
    _, registry, confirmed = setup_registry(tmp_path)
    sid = confirmed['strategy_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _: registry.record(sid, 2, manifest()), range(2)))
    assert records[0] == records[1]
    assert registry.list_runs(sid, 2) == [records[0]]
    registry.strategies.save(confirmed['definition'], strategy_id=sid, expected_version=2)
    registry.strategies.confirm(sid, expected_version=3, confirmed_by='tester')
    supplied = manifest()
    supplied['summary'] = 'new version result'
    new = registry.record(sid, 4, supplied)
    assert registry.list_runs(sid, 4) == [new]
    assert registry.list_runs(sid, 2) == [records[0]]


@pytest.mark.parametrize('version', [None, True, 2.0, 0, -1, 1, 999])
def test_requires_exact_existing_confirmed_version(tmp_path, version):
    _, registry, confirmed = setup_registry(tmp_path)
    with pytest.raises((ValueError, KeyError)):
        registry.record(confirmed['strategy_id'], version, manifest())
    assert registry.list_runs(confirmed['strategy_id'], 2) == []


@pytest.mark.parametrize(('path', 'bad'), [
    (('run_id',), '../run'), (('stage',), []), (('outcome',), 'success'),
    (('recorded_by',), ''), (('assumptions',), []), (('limitations',), ['']),
    (('implementation', 'revision'), ''), (('parameters', 'lookback', 'source'), ''),
    (('data',), []), (('data', 0, 'sha256'), 'not-a-hash'),
    (('period', 'end'), '2019-12-01'), (('period', 'start'), 'bad'),
    (('started_at',), '2026-11-09T10:00:00'),
    (('finished_at',), '2026-11-09T01:00:00+00:00'),
    (('duration_seconds',), -1), (('duration_seconds',), True),
    (('duration_seconds',), float('inf')),
    (('metrics', 'return', 'value'), float('nan')),
    (('metrics', 'return', 'value'), True), (('metrics', 'return', 'unit'), ''),
])
def test_invalid_manifests_leave_no_partial_record(tmp_path, path, bad):
    _, registry, confirmed = setup_registry(tmp_path)
    supplied = copy.deepcopy(manifest())
    target = supplied
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    with pytest.raises(ValueError):
        registry.record(confirmed['strategy_id'], 2, supplied)
    assert registry.list_runs(confirmed['strategy_id'], 2) == []


def test_bridge_errors_and_empty_metrics_are_explicit(tmp_path):
    store, _, confirmed = setup_registry(tmp_path)
    api = DesktopAPI(store)
    supplied = manifest()
    supplied['metrics'] = {}
    supplied['parameters'] = {}
    assert not api.record_strategy_run('missing', 2, supplied)['ok']
    assert not api.record_strategy_run(confirmed['strategy_id'], 1, supplied)['ok']
    assert not api.record_strategy_run(confirmed['strategy_id'], 2, None)['ok']
    saved = api.record_strategy_run(confirmed['strategy_id'], 2, supplied)
    assert saved['ok']
    assert api.list_strategy_runs(confirmed['strategy_id'], 2)['data'] == [saved['data']]
    assert not api.list_strategy_runs(confirmed['strategy_id'], None)['ok']
