import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import RULE_FIELDS, SCOPE_FIELDS
from tradingagents.desktop.strategy_checks import StrategyCheckRegistry, validate_data

AS_OF = '2020-01-04T16:00:00+08:00'


def comparison(op='gt', left=None, right=None):
    return {'op': op, 'left': left or {'metric': 'close'},
            'right': right or {'constant': 20, 'unit': 'CNY/share', 'source': '测试假设'}}


def observation(day, value, available=None):
    return {'observed_at': f'2020-01-{day:02d}T15:00:00+08:00',
            'available_at': available or f'2020-01-{day:02d}T15:05:00+08:00', 'value': value}


def dataset():
    return {'symbol': '000001.SZ', 'market': 'CN-A', 'timeframe': 'daily',
            'price_basis': 'raw', 'source': 'synthetic', 'revision': 'fixture-v1',
            'series': {'close': {'unit': 'CNY/share', 'observations': [
                observation(2, 10), observation(3, 20), observation(4, 30)]}}}


def setup_registry(tmp_path, expressions=None):
    registry = StrategyCheckRegistry(DesktopStore(tmp_path))
    definition = {**dict.fromkeys(SCOPE_FIELDS, '测试假设'), 'market': 'CN-A',
                  'timeframe': 'daily', 'rules': {
                      key: [{'text': '人工核对', 'citation': '个人测试假设',
                             'kind': 'personal', 'state': 'manual'}] for key in RULE_FIELDS}}
    definition['rules']['entry'] = [
        {'text': '测试比较', 'citation': '个人测试假设', 'kind': 'personal',
         'state': 'defined', 'check': expr} for expr in
        (expressions if expressions is not None else [comparison()])]
    draft = registry.strategies.save(definition)
    confirmed = registry.strategies.confirm(draft['strategy_id'], expected_version=1,
                                            confirmed_by='tester')
    return registry, confirmed


def run(registry, confirmed, data=None, as_of=AS_OF):
    return registry.check(confirmed['strategy_id'], confirmed['version'], as_of=as_of,
                          data=dataset() if data is None else data)


@pytest.mark.parametrize(('op', 'status'), [
    ('gt', 'met'), ('ge', 'met'), ('lt', 'not_met'), ('le', 'not_met'), ('eq', 'not_met')])
def test_comparisons_preserve_evidence_and_do_not_certify_strategy(tmp_path, op, status):
    registry, confirmed = setup_registry(tmp_path, [comparison(op)])
    record = run(registry, confirmed)
    condition = record['conditions'][0]
    assert condition['status'] == status
    assert condition['left']['value'] == '30'
    assert condition['left']['observations'] == [observation(4, 30)]
    assert condition['right']['source'] == '测试假设'
    assert record['counts'] == {status: 1, 'manual': 6}
    assert record['evidence_status'] == 'caller_supplied_unverified'
    assert record['data'] == dataset()
    assert registry.strategies.get(confirmed['strategy_id'], 2) == confirmed
    assert confirmed['implementation'] == 'not_implemented'
    assert confirmed['backtest'] == confirmed['out_of_sample'] == 'not_evaluated'


def test_point_in_time_revision_and_timezone_selection(tmp_path):
    mean = {'metric': 'close', 'aggregate': 'mean', 'window': 2}
    registry, confirmed = setup_registry(tmp_path, [comparison(left=mean)])
    data = dataset()
    rows = data['series']['close']['observations']
    rows.extend([observation(3, 100, '2020-01-05T01:00:00+08:00'), observation(5, 999)])
    early = run(registry, confirmed, data)
    assert early['conditions'][0]['left']['value'] == '25'
    # Equivalent UTC cutoff selects the same values, while retaining the supplied timestamp.
    utc = run(registry, confirmed, data, '2020-01-04T08:00:00Z')
    assert utc['conditions'] == early['conditions']
    later = run(registry, confirmed, data, '2020-01-05T02:00:00+08:00')
    assert later['conditions'][0]['left']['value'] == '65'
    assert later['conditions'][0]['left']['observations'][0]['value'] == 100


@pytest.mark.parametrize('value', [None, 0])
def test_new_missing_revision_is_not_replaced_by_previous_value(tmp_path, value):
    registry, confirmed = setup_registry(tmp_path)
    data = dataset()
    data['series']['close']['observations'].append(
        observation(4, value, '2020-01-04T15:30:00+08:00'))
    condition = run(registry, confirmed, data)['conditions'][0]
    assert condition['status'] == ('insufficient_data' if value is None else 'not_met')
    if value == 0:
        assert condition['left']['value'] == '0'


@pytest.mark.parametrize('operand', [
    {'metric': 'missing'}, {'metric': 'close', 'aggregate': 'mean', 'window': 4}])
def test_insufficient_data_is_not_false(tmp_path, operand):
    registry, confirmed = setup_registry(tmp_path, [comparison(left=operand)])
    assert run(registry, confirmed)['conditions'][0]['status'] == 'insufficient_data'


def test_mean_rejects_missing_in_window_instead_of_skipping(tmp_path):
    registry, confirmed = setup_registry(tmp_path, [comparison(
        left={'metric': 'close', 'aggregate': 'mean', 'window': 2})])
    data = dataset()
    data['series']['close']['observations'][1]['value'] = None
    assert run(registry, confirmed, data)['conditions'][0]['status'] == 'insufficient_data'


def test_large_numbers_and_recurring_means_are_compared_exactly(tmp_path):
    huge = 10**40
    registry, confirmed = setup_registry(tmp_path, [comparison('gt', right={
        'constant': huge, 'unit': 'CNY/share', 'source': 'synthetic'})])
    data = dataset()
    data['series']['close']['observations'][-1]['value'] = huge + 1
    assert run(registry, confirmed, data)['conditions'][0]['status'] == 'met'
    mean = {'metric': 'close', 'aggregate': 'mean', 'window': 3}
    registry2, confirmed2 = setup_registry(tmp_path / 'mean', [comparison('gt', left=mean,
        right={'constant': 0.3333333333333333, 'unit': 'CNY/share', 'source': 'synthetic'})])
    data['series']['close']['observations'] = [observation(2, 0), observation(3, 0),
                                               observation(4, 1)]
    condition = run(registry2, confirmed2, data)['conditions'][0]
    assert condition['status'] == 'met'
    assert condition['left']['value'] == '1/3'


@pytest.mark.parametrize(('expression', 'status'), [
    (None, 'not_implemented'),
    (comparison('crosses_above'), 'not_implemented'),
    (comparison(left={'indicator': 'RSI'}), 'not_implemented'),
    ({'op': 'gt'}, 'invalid_rule'),
    (comparison(right={'constant': 1, 'unit': 'USD/share', 'source': 'test'}), 'invalid_rule'),
    (comparison(right={'constant': 1, 'unit': 'CNY/share'}), 'invalid_rule'),
    (comparison(left={'metric': 'close', 'aggregate': 'mean', 'window': True}), 'invalid_rule'),
    (comparison(left={'metric': []}), 'invalid_rule'),
])
def test_unimplemented_and_invalid_rules_are_explicit(tmp_path, expression, status):
    registry, confirmed = setup_registry(tmp_path, [expression])
    assert run(registry, confirmed)['conditions'][0]['status'] == status


def test_manual_and_invalidation_truth_do_not_become_instructions(tmp_path):
    registry, confirmed = setup_registry(tmp_path)
    definition = copy.deepcopy(confirmed['definition'])
    definition['rules']['entry'][0]['state'] = 'manual'
    definition['rules']['invalidation'] = [dict(definition['rules']['entry'][0], state='defined')]
    registry.strategies.save(definition, strategy_id=confirmed['strategy_id'], expected_version=2)
    next_version = registry.strategies.confirm(confirmed['strategy_id'], expected_version=3,
                                               confirmed_by='tester')
    result = run(registry, next_version)
    assert result['conditions'][0]['status'] == 'manual'
    assert next(row for row in result['conditions']
                if row['group'] == 'invalidation')['status'] == 'met'
    assert 'action' not in result and 'passed' not in result


@pytest.mark.parametrize('version', [1, True, 2.0, None, 999])
def test_only_exact_confirmed_version_can_be_checked(tmp_path, version):
    registry, confirmed = setup_registry(tmp_path)
    with pytest.raises(ValueError):
        registry.check(confirmed['strategy_id'], version, as_of=AS_OF, data=dataset())


@pytest.mark.parametrize('field', ['market', 'timeframe'])
def test_scope_mismatch_is_rejected(tmp_path, field):
    registry, confirmed = setup_registry(tmp_path)
    data = dataset()
    data[field] = 'other'
    with pytest.raises(ValueError, match='不一致'):
        run(registry, confirmed, data)


@pytest.mark.parametrize('value', [True, float('nan'), float('inf'), '30', {}])
def test_invalid_values_are_not_coerced(value):
    data = dataset()
    data['series']['close']['observations'][0]['value'] = value
    with pytest.raises(ValueError):
        validate_data(data)


@pytest.mark.parametrize('timestamp', ['2020-01-04', '2020-01-04T16:00:00', 'bad', None])
def test_naive_or_invalid_check_time_is_rejected(tmp_path, timestamp):
    registry, confirmed = setup_registry(tmp_path)
    with pytest.raises(ValueError, match='时区'):
        run(registry, confirmed, as_of=timestamp)


def test_invalid_availability_and_duplicate_instants_are_rejected():
    data = dataset()
    rows = data['series']['close']['observations']
    rows[0]['available_at'] = '2020-01-02T06:59:59Z'
    with pytest.raises(ValueError, match='早于'):
        validate_data(data)
    rows[0]['available_at'] = '2020-01-02T15:05:00+08:00'
    rows.append({'observed_at': '2020-01-02T07:00:00Z',
                 'available_at': '2020-01-02T07:05:00Z', 'value': 99})
    with pytest.raises(ValueError, match='重复'):
        validate_data(data)


def test_required_metadata_and_observation_limit(monkeypatch):
    for field in dataset():
        data = dataset()
        del data[field]
        with pytest.raises(ValueError):
            validate_data(data)
    monkeypatch.setattr('tradingagents.desktop.strategy_checks.MAX_OBSERVATIONS', 2)
    with pytest.raises(ValueError, match='上限'):
        validate_data(dataset())


def test_snapshot_idempotency_concurrency_restart_and_version_isolation(tmp_path):
    registry, confirmed = setup_registry(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _: run(registry, confirmed), range(2)))
    assert records[0] == records[1]
    original = copy.deepcopy(records[0])
    records[0]['data']['revision'] = 'caller mutation'
    records[0]['conditions'].clear()
    data = dataset()
    changed = run(registry, confirmed, data)
    data['series']['close']['observations'].clear()
    reopened = StrategyCheckRegistry(DesktopStore(tmp_path))
    assert reopened.get(original['check_id']) == original == changed
    sid = confirmed['strategy_id']
    reopened.strategies.save(confirmed['definition'], strategy_id=sid, expected_version=2)
    assert reopened.history(sid, 3) == []
    history = reopened.history(sid, 2)
    assert len(history) == 1 and history[0]['check_id'] == original['check_id']
    assert 'data' not in history[0] and 'conditions' not in history[0]
    # An older confirmed definition remains usable after a new draft exists.
    assert run(reopened, confirmed) == original
    updated = dataset()
    updated['revision'] = 'fixture-v2'
    assert run(reopened, confirmed, updated)['check_id'] != original['check_id']
    with pytest.raises(ValueError):
        reopened.get('missing')
    with pytest.raises(ValueError):
        reopened.history(sid, True)


def test_desktop_bridge_roundtrip_and_validation_errors(tmp_path):
    registry, confirmed = setup_registry(tmp_path)
    api = DesktopAPI(registry.store)
    sid = confirmed['strategy_id']
    result = api.check_strategy_conditions(sid, 2, AS_OF, dataset())
    assert result['ok']
    record = result['data']
    assert api.get_strategy_check(record['check_id'])['data'] == record
    assert api.strategy_check_history(sid, 2)['data'][0]['check_id'] == record['check_id']
    assert not api.check_strategy_conditions(sid, 1, AS_OF, dataset())['ok']
    assert not api.check_strategy_conditions(sid, 2, AS_OF, {})['ok']
    assert not api.get_strategy_check('missing')['ok']


@pytest.mark.parametrize(('seconds', 'status'), [(3599, 'stale_data'), (3600, 'met'),
                                               (3601, 'met'), (0, 'stale_data')])
def test_explicit_observation_age_limit_and_boundary(tmp_path, seconds, status):
    registry, confirmed = setup_registry(tmp_path, [comparison(
        left={'metric': 'close', 'max_age_seconds': seconds})])
    record = run(registry, confirmed)
    condition = record['conditions'][0]
    assert condition['status'] == status
    assert condition['left']['freshness'] == {
        'status': 'stale' if status == 'stale_data' else 'within_limit',
        'latest_observed_at': observation(4, 30)['observed_at'],
        'age_seconds': 3600.0, 'max_age_seconds': seconds}
    assert condition['left']['observations'] == [observation(4, 30)]
    assert condition['right']['value'] == '20'
    assert registry.get(record['check_id']) == record
    assert registry.strategies.get(confirmed['strategy_id'], 2) == confirmed


def test_unconfigured_age_is_explicitly_unevaluated_and_old_snapshots_survive(tmp_path):
    registry, confirmed = setup_registry(tmp_path)
    record = run(registry, confirmed, as_of='2021-01-04T16:00:00+08:00')
    assert record['conditions'][0]['status'] == 'met'
    age = record['conditions'][0]['left']['freshness']
    assert age['status'] == 'not_evaluated' and age['max_age_seconds'] is None
    # Existing v1 evidence remains verbatim, not silently reclassified by the new engine.
    old = copy.deepcopy(record)
    old.update(engine='stockseek-conditions/1', check_id='old-engine-snapshot')
    del old['conditions'][0]['left']['freshness']
    with registry.store.connect() as db:
        db.execute('INSERT INTO strategy_condition_checks VALUES (?, ?, ?, ?)',
                   (old['check_id'], old['strategy_id'], old['version'], json.dumps(old)))
    assert registry.get(old['check_id']) == old
    assert run(registry, confirmed)['check_id'] != old['check_id']


def test_mean_age_uses_latest_observation_not_revision_or_oldest_window_member(tmp_path):
    mean = {'metric': 'close', 'aggregate': 'mean', 'window': 3, 'max_age_seconds': 3600}
    registry, confirmed = setup_registry(tmp_path, [comparison('ge', right=mean)])
    data = dataset()
    data['series']['close']['observations'].append(
        observation(4, 25, '2020-01-04T16:00:00+08:00'))
    result = run(registry, confirmed, data, '2020-01-04T08:00:00Z')['conditions'][0]
    assert result['status'] == 'met'
    assert result['right']['freshness']['age_seconds'] == 3600
    later = run(registry, confirmed, data, '2020-01-04T16:00:00.000001+08:00')
    assert later['conditions'][0]['status'] == 'stale_data'
    assert later['conditions'][0]['right']['freshness']['age_seconds'] > 3600


@pytest.mark.parametrize('value', [None, True, False, -1, 31536001, 1.5, '3600', {}, []])
def test_invalid_age_policy_cannot_produce_a_match(tmp_path, value):
    registry, confirmed = setup_registry(tmp_path, [comparison(
        left={'metric': 'close', 'max_age_seconds': value})])
    assert run(registry, confirmed)['conditions'][0]['status'] == 'invalid_rule'


def test_age_never_makes_future_or_missing_data_usable(tmp_path):
    registry, confirmed = setup_registry(tmp_path, [comparison(
        left={'metric': 'close', 'max_age_seconds': 3600})])
    data = dataset()
    data['series']['close']['observations'][-1]['value'] = None
    assert run(registry, confirmed, data)['conditions'][0]['status'] == 'insufficient_data'
    data['series']['close']['observations'] = [observation(5, 999)]
    assert run(registry, confirmed, data)['conditions'][0]['status'] == 'insufficient_data'


def test_zero_age_same_instant_and_delayed_revision_not_freshening_old_observation(tmp_path):
    registry, confirmed = setup_registry(tmp_path, [comparison(
        left={'metric': 'close', 'max_age_seconds': 0})])
    data = dataset()
    data['series']['close']['observations'][-1]['available_at'] = observation(4, 30)['observed_at']
    result = run(registry, confirmed, data, '2020-01-04T07:00:00Z')['conditions'][0]
    assert result['status'] == 'met' and result['left']['freshness']['age_seconds'] == 0
    data['series']['close']['observations'].append(
        observation(4, 40, '2020-01-04T16:00:00+08:00'))
    result = run(registry, confirmed, data)['conditions'][0]
    assert result['status'] == 'stale_data'
    assert result['left']['value'] == '40'
    assert result['left']['freshness']['age_seconds'] == 3600


def test_stale_threshold_cannot_hide_unit_error_or_make_constant_age_valid(tmp_path):
    registry, confirmed = setup_registry(tmp_path, [
        comparison(left={'metric': 'close', 'max_age_seconds': 0},
                   right={'constant': 20, 'unit': 'USD/share', 'source': 'test'}),
        comparison(right={'constant': 20, 'unit': 'CNY/share', 'source': 'test',
                          'max_age_seconds': 3600})])
    assert all(row['status'] == 'invalid_rule' for row in
               run(registry, confirmed)['conditions'][:2])


def test_age_policy_change_requires_new_confirmed_version_and_snapshot(tmp_path):
    registry, confirmed = setup_registry(tmp_path)
    original = run(registry, confirmed)
    definition = copy.deepcopy(confirmed['definition'])
    definition['rules']['entry'][0]['check']['left']['max_age_seconds'] = 3599
    sid = confirmed['strategy_id']
    registry.strategies.save(definition, strategy_id=sid, expected_version=2)
    with pytest.raises(ValueError, match='确认'):
        registry.check(sid, 3, as_of=AS_OF, data=dataset())
    updated = registry.strategies.confirm(sid, expected_version=3, confirmed_by='tester')
    result = run(registry, updated)
    assert result['conditions'][0]['status'] == 'stale_data'
    assert result['check_id'] != original['check_id']
    assert run(registry, confirmed) == original
