import copy

import pytest

from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import RULE_FIELDS, SCOPE_FIELDS, StrategyRegistry


def complete_definition():
    return {**dict.fromkeys(SCOPE_FIELDS, '测试假设，待用户核对'), 'rules': {
        key: [{'text': '人工检查条件', 'citation': '个人假设，不来自原书',
               'kind': 'personal', 'state': 'manual'}] for key in RULE_FIELDS}}


def test_versions_survive_restart_and_edits_reset_confirmation(tmp_path):
    registry = StrategyRegistry(DesktopStore(tmp_path))
    definition = complete_definition()
    draft = registry.save(definition)
    definition['name'] = 'caller mutation'
    confirmed = registry.confirm(draft['strategy_id'], expected_version=1, confirmed_by='tester')
    assert confirmed['status'] == 'confirmed'
    assert confirmed['implementation'] == 'not_implemented'
    assert confirmed['backtest'] == confirmed['out_of_sample'] == 'not_evaluated'
    edited = registry.save(confirmed['definition'], strategy_id=draft['strategy_id'],
                           expected_version=2)
    assert edited['status'] == 'draft' and edited['confirmation'] is None
    reopened = StrategyRegistry(DesktopStore(tmp_path))
    assert reopened.get(draft['strategy_id'], 1) == draft
    assert reopened.get(draft['strategy_id'], 2) == confirmed
    assert reopened.get(draft['strategy_id']) == edited


def test_incomplete_or_pending_cannot_be_confirmed(tmp_path):
    registry = StrategyRegistry(DesktopStore(tmp_path))
    for definition in ({'name': 'partial'}, complete_definition()):
        if 'rules' in definition:
            definition['rules']['entry'][0]['state'] = 'pending'
        draft = registry.save(definition)
        with pytest.raises(ValueError):
            registry.confirm(draft['strategy_id'], expected_version=1, confirmed_by='tester')
        assert registry.get(draft['strategy_id']) == draft


def test_stale_writer_cannot_overwrite_or_confirm(tmp_path):
    registry = StrategyRegistry(DesktopStore(tmp_path))
    draft = registry.save(complete_definition())
    registry.save(draft['definition'], strategy_id=draft['strategy_id'], expected_version=1)
    with pytest.raises(ValueError):
        registry.save(draft['definition'], strategy_id=draft['strategy_id'], expected_version=1)
    with pytest.raises(ValueError):
        registry.confirm(draft['strategy_id'], expected_version=1, confirmed_by='tester')
    assert registry.get(draft['strategy_id'])['version'] == 2


@pytest.mark.parametrize('rule', [
    {'text': '无出处', 'kind': 'author', 'state': 'defined'},
    {'text': '伪状态', 'citation': 'test', 'kind': 'personal', 'state': 'executed'},
])
def test_invalid_provenance_rejected(tmp_path, rule):
    definition = copy.deepcopy(complete_definition())
    definition['rules']['entry'] = [rule]
    with pytest.raises(ValueError):
        StrategyRegistry(DesktopStore(tmp_path)).save(definition)


def test_nonfinite_parameters_rejected(tmp_path):
    definition = complete_definition()
    definition['parameters'] = {'threshold': float('nan')}
    with pytest.raises(ValueError):
        StrategyRegistry(DesktopStore(tmp_path)).save(definition)


def test_desktop_bridge_returns_versions_and_validation_errors(tmp_path):
    from tradingagents.desktop.api import DesktopAPI

    api = DesktopAPI(DesktopStore(tmp_path))
    saved = api.save_strategy(complete_definition())
    assert saved['ok']
    draft = saved['data']
    assert api.get_strategy(draft['strategy_id'], 1)['data'] == draft
    assert api.confirm_strategy(draft['strategy_id'], 1, 'tester')['ok']
    assert not api.confirm_strategy(draft['strategy_id'], 1, 'tester')['ok']
    assert not api.save_strategy({'name': ''})['ok']


def test_lists_return_latest_versions_and_complete_history(tmp_path):
    from tradingagents.desktop.api import DesktopAPI

    api = DesktopAPI(DesktopStore(tmp_path))
    assert api.list_strategies()['data'] == []
    first = api.save_strategy(complete_definition())['data']
    second = api.save_strategy({'name': 'another'})['data']
    confirmed = api.confirm_strategy(first['strategy_id'], 1, 'tester')['data']
    listed = api.list_strategies()['data']
    assert {row['strategy_id']: row for row in listed} == {
        first['strategy_id']: confirmed, second['strategy_id']: second}
    assert api.strategy_history(first['strategy_id'])['data'] == [confirmed, first]
    assert not api.strategy_history('missing')['ok']
    listed[0]['definition']['name'] = 'caller mutation'
    assert api.get_strategy(first['strategy_id'], 1)['data'] == first
