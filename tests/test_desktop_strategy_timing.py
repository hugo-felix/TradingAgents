import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import RULE_FIELDS, SCOPE_FIELDS, StrategyRegistry
from tradingagents.desktop.strategy_timing import StrategyTimingRegistry, main, validate_evidence


def contract():
    return {'observation': 'completed_bar', 'confirmation_delay_seconds': 60,
            'execution': 'next_session_open', 'price_basis': 'raw', 'price_unit': 'CNY/share'}


def evidence():
    return {'calendar': {'market': 'CN-A', 'source': 'synthetic calendar', 'revision': 'v1',
                         'complete': True, 'coverage_start': '2020-01-03T00:00:00+08:00',
                         'coverage_end': '2020-01-07T00:00:00+08:00', 'sessions': [
                             {'open_at': '2020-01-06T09:30:00+08:00',
                              'close_at': '2020-01-06T15:00:00+08:00'},
                             {'open_at': '2020-01-03T09:30:00+08:00',
                              'close_at': '2020-01-03T15:00:00+08:00'}]},
            'execution_price': {'at': '2020-01-06T09:30:00+08:00',
                                'available_at': '2020-01-06T09:30:01+08:00', 'value': 11,
                                'unit': 'CNY/share', 'price_basis': 'raw',
                                'source': 'synthetic opening quote', 'revision': 'v1'}}


def setup(tmp_path, *, as_of='2020-01-03T16:00:00+08:00', timing=True, manual=False):
    store = DesktopStore(tmp_path)
    registry = StrategyTimingRegistry(store)
    definition = {**dict.fromkeys(SCOPE_FIELDS, '人工测试假设'), 'market': 'CN-A',
                  'timeframe': 'daily', 'rules': {
                      key: [{'text': '人工检查', 'citation': '个人测试假设', 'kind': 'personal',
                             'state': 'manual'}] for key in RULE_FIELDS}}
    if timing:
        definition['timing'] = contract()
    if not manual:
        definition['rules']['entry'][0].update(state='defined', check={
            'op': 'gt', 'left': {'metric': 'close'},
            'right': {'constant': 9, 'unit': 'CNY/share', 'source': 'personal test'}})
    draft = registry.checks.strategies.save(definition)
    strategy = registry.checks.strategies.confirm(draft['strategy_id'], expected_version=1,
                                                 confirmed_by='tester')
    data = {'symbol': '000001.SZ', 'market': 'CN-A', 'timeframe': 'daily', 'price_basis': 'raw',
            'source': 'synthetic', 'revision': 'v1', 'series': {'close': {
                'unit': 'CNY/share', 'observations': [
                    {'observed_at': '2020-01-03T15:00:00+08:00',
                     'available_at': '2020-01-03T15:05:00+08:00', 'value': 10},
                    {'observed_at': '2020-01-03T15:00:00+08:00',
                     'available_at': '2020-01-07T15:05:00+08:00', 'value': 99}]}}}
    check = registry.checks.check(strategy['strategy_id'], 2, as_of=as_of, data=data)
    return registry, strategy, check


def test_weekend_next_open_exact_version_and_immutable_evidence(tmp_path):
    registry, strategy, check = setup(tmp_path)
    supplied = evidence()
    original = copy.deepcopy(supplied)
    record = registry.assess(check['check_id'], supplied)
    assert record['result'] == {
        'status': 'consistent', 'reasons': [], 'observation_at': '2020-01-03T15:00:00+08:00',
        'latest_input_available_at': '2020-01-03T15:05:00+08:00',
        'confirmed_at': check['as_of'], 'expected_execution_at': '2020-01-06T09:30:00+08:00'}
    assert record['evidence_status'] == 'caller_supplied_unverified'
    assert record['condition_counts'] == {'met': 1, 'manual': 6}
    assert 'fill' not in record and 'passed' not in record
    supplied['execution_price']['value'] = 900
    record['contract']['price_basis'] = 'mutated'
    reopened = StrategyTimingRegistry(DesktopStore(tmp_path))
    receipt = reopened.get(record['audit_id'])
    assert receipt['evidence'] == original
    assert receipt['contract'] == contract()
    assert reopened.assess(check['check_id'], original) == receipt
    draft = reopened.checks.strategies.save(strategy['definition'],
                                           strategy_id=strategy['strategy_id'], expected_version=2)
    assert draft['version'] == 3
    assert reopened.assess(check['check_id'], original) == receipt
    assert reopened.checks.get(check['check_id']) == check
    assert reopened.checks.strategies.get(strategy['strategy_id'], 2) == strategy
    assert reopened.history(check['check_id'])[0]['audit_id'] == receipt['audit_id']
    assert 'evidence' not in reopened.history(check['check_id'])[0]
    changed = copy.deepcopy(original)
    changed['execution_price']['revision'] = 'v2'
    assert reopened.assess(check['check_id'], changed)['audit_id'] != receipt['audit_id']
    assert len(reopened.history(check['check_id'])) == 2


@pytest.mark.parametrize('at', ['2020-01-03T15:00:00+08:00', '2020-01-03T16:00:00+08:00',
                               '2020-01-06T10:00:00+08:00'])
def test_close_same_instant_and_later_intraday_prices_cannot_be_next_open(tmp_path, at):
    registry, _, check = setup(tmp_path)
    data = evidence()
    data['execution_price']['at'] = at
    data['execution_price']['available_at'] = '2020-01-06T16:00:00+08:00'
    assert registry.assess(check['check_id'], data)['result']['status'] == 'inconsistent'


def test_confirmation_delay_includes_input_publication_delay(tmp_path):
    registry, _, check = setup(tmp_path, as_of='2020-01-03T15:05:30+08:00')
    assert registry.assess(check['check_id'], evidence())['result']['status'] == 'inconsistent'
    registry2, _, boundary = setup(tmp_path / 'boundary', as_of='2020-01-03T15:06:00+08:00')
    assert registry2.assess(boundary['check_id'], evidence())['result']['status'] == 'consistent'


def test_equivalent_timezones_compare_as_instants(tmp_path):
    registry, _, check = setup(tmp_path, as_of='2020-01-03T08:00:00Z')
    data = evidence()
    data['execution_price']['at'] = '2020-01-06T01:30:00Z'
    assert registry.assess(check['check_id'], data)['result']['status'] == 'consistent'


def test_confirmation_exactly_at_open_requires_following_session(tmp_path):
    registry, _, check = setup(tmp_path, as_of='2020-01-06T09:30:00+08:00')
    result = registry.assess(check['check_id'], evidence())['result']
    assert result['status'] == 'inconsistent'
    assert result['expected_execution_at'] is None
    data = evidence()
    data['calendar']['coverage_end'] = '2020-01-08T00:00:00+08:00'
    data['calendar']['sessions'].append({'open_at': '2020-01-07T09:30:00+08:00',
                                         'close_at': '2020-01-07T15:00:00+08:00'})
    data['execution_price'].update(at='2020-01-07T09:30:00+08:00',
                                   available_at='2020-01-07T09:30:01+08:00')
    assert registry.assess(check['check_id'], data)['result']['status'] == 'consistent'


def test_timing_consistency_does_not_promote_unmet_conditions(tmp_path):
    registry, strategy, check = setup(tmp_path)
    data = copy.deepcopy(check['data'])
    data['series']['close']['observations'][0]['value'] = 8
    unmet = registry.checks.check(strategy['strategy_id'], 2, as_of=check['as_of'], data=data)
    audit = registry.assess(unmet['check_id'], evidence())
    assert audit['result']['status'] == 'consistent'
    assert audit['condition_counts'] == {'not_met': 1, 'manual': 6}
    assert registry.checks.get(unmet['check_id']) == unmet


@pytest.mark.parametrize('missing', ['calendar', 'coverage', 'sessions', 'price', 'observations'])
def test_missing_evidence_never_becomes_consistent(tmp_path, missing):
    registry, _, check = setup(tmp_path, manual=missing == 'observations')
    data = evidence()
    if missing == 'calendar':
        data['calendar']['complete'] = False
    elif missing == 'coverage':
        data['calendar']['coverage_start'] = '2020-01-03T17:00:00+08:00'
        data['calendar']['sessions'] = data['calendar']['sessions'][:1]
    elif missing == 'sessions':
        data['calendar']['sessions'] = []
    elif missing == 'price':
        data['execution_price'] = None
    assert registry.assess(check['check_id'], data)['result']['status'] == 'insufficient_data'


@pytest.mark.parametrize(('field', 'value'), [('price_basis', 'adjusted'), ('unit', 'USD/share')])
def test_execution_price_contract_mismatch_is_preserved_as_inconsistent(tmp_path, field, value):
    registry, _, check = setup(tmp_path)
    data = evidence()
    data['execution_price'][field] = value
    assert registry.assess(check['check_id'], data)['result']['status'] == 'inconsistent'


def test_condition_data_basis_must_match_confirmed_contract(tmp_path):
    registry, strategy, check = setup(tmp_path)
    data = check['data']
    data['price_basis'] = 'adjusted'
    check2 = registry.checks.check(strategy['strategy_id'], 2, as_of=check['as_of'], data=data)
    assert registry.assess(check2['check_id'], evidence())['result']['status'] == 'inconsistent'


@pytest.mark.parametrize('change', ['overlap', 'duplicate', 'reverse', 'outside', 'market',
                                   'naive', 'boolean', 'zero', 'future', 'unknown', 'complete'])
def test_malformed_calendar_or_price_is_rejected(change):
    data = evidence()
    sessions = data['calendar']['sessions']
    if change == 'overlap':
        sessions.append({'open_at': '2020-01-03T10:00:00+08:00',
                         'close_at': '2020-01-03T11:00:00+08:00'})
    elif change == 'duplicate':
        sessions.append({'open_at': '2020-01-03T01:30:00Z',
                         'close_at': '2020-01-03T07:00:00Z'})
    elif change == 'reverse':
        sessions[0]['close_at'] = sessions[0]['open_at']
    elif change == 'outside':
        data['calendar']['coverage_end'] = '2020-01-06T12:00:00+08:00'
    elif change == 'market':
        data['calendar']['market'] = 'other'
    elif change == 'naive':
        data['execution_price']['at'] = '2020-01-06T09:30:00'
    elif change == 'boolean':
        data['execution_price']['value'] = True
    elif change == 'zero':
        data['execution_price']['value'] = 0
    elif change == 'future':
        data['execution_price']['available_at'] = '2020-01-03T01:00:00Z'
    elif change == 'unknown':
        data['calendar']['timezone'] = 'UTC'
    else:
        data['calendar']['complete'] = 'yes'
    with pytest.raises(ValueError):
        validate_evidence(data, 'CN-A')


@pytest.mark.parametrize('field,value', [('observation', 'intraday'), ('execution', 'same_close'),
                                        ('confirmation_delay_seconds', True),
                                        ('confirmation_delay_seconds', -1), ('price_unit', '')])
def test_unsupported_timing_contract_cannot_be_saved(tmp_path, field, value):
    definition = {'name': 'test', 'timing': contract()}
    definition['timing'][field] = value
    with pytest.raises(ValueError):
        StrategyRegistry(DesktopStore(tmp_path)).save(definition)


def test_legacy_prose_has_no_implied_contract_and_bridge_errors(tmp_path):
    registry, _, check = setup(tmp_path, timing=False)
    with pytest.raises(ValueError):
        registry.assess(check['check_id'], evidence())
    api = DesktopAPI(registry.store)
    assert not api.assess_strategy_timing(check['check_id'], evidence())['ok']
    assert not api.get_strategy_timing('missing')['ok']
    assert not api.strategy_timing_history('missing')['ok']


def test_concurrent_retries_keep_one_snapshot_and_api_can_read_it(tmp_path):
    registry, _, check = setup(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: registry.assess(check['check_id'], evidence()), range(8)))
    assert all(row == receipts[0] for row in receipts)
    api = DesktopAPI(registry.store)
    assert api.assess_strategy_timing(check['check_id'], evidence())['data'] == receipts[0]
    assert api.get_strategy_timing(receipts[0]['audit_id'])['data'] == receipts[0]
    assert len(api.strategy_timing_history(check['check_id'])['data']) == 1


def test_cli_roundtrip_closed_workspace_and_backup(tmp_path, capsys):
    from tradingagents.desktop.backup import create_backup, restore_backup

    workspace = tmp_path / 'workspace'
    registry, _, check = setup(workspace)
    supplied = tmp_path / 'evidence.json'
    supplied.write_text(json.dumps(evidence()), encoding='utf-8-sig')
    args = ['--workspace', str(workspace)]
    main([*args, 'assess', check['check_id'], str(supplied)])
    receipt = json.loads(capsys.readouterr().out)
    main([*args, 'get', receipt['audit_id']])
    assert json.loads(capsys.readouterr().out) == receipt
    main([*args, 'history', check['check_id']])
    assert len(json.loads(capsys.readouterr().out)) == 1
    archive = tmp_path / 'backup.zip'
    create_backup(workspace, archive)
    restored = tmp_path / 'restored'
    restore_backup(archive, restored)
    assert StrategyTimingRegistry(DesktopStore(restored)).get(receipt['audit_id']) == receipt
    assert registry.checks.get(check['check_id']) == check


@pytest.mark.parametrize('state', ['missing', 'incomplete', 'locked'])
def test_cli_does_not_open_invalid_or_active_workspace(tmp_path, state, capsys):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock

    if state != 'missing':
        DesktopStore(tmp_path)
    if state == 'incomplete':
        (tmp_path / INCOMPLETE).touch()
    with WorkspaceLock(tmp_path) if state == 'locked' else nullcontext():
        with pytest.raises(SystemExit) as exc:
            main(['--workspace', str(tmp_path), 'get', 'missing'])
        assert exc.value.code == 1
        assert '操作失败' in capsys.readouterr().err
