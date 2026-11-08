import copy
from datetime import date

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.planning import entry_candidates, strategy_plan, validate_preferences
from tradingagents.desktop.store import DesktopStore


def preferences():
    return {'capital': 100000, 'target_yield': 5, 'position_pct': 20, 'horizon_years': 3}


def candidate(code='600941', yield_pct=5.2, dps=4.999):
    return {'code': code, 'name': '离线测试', 'sector': '测试', 'status': 'pass',
            'core': {'status': 'pass'},
            'metrics': {'yield_pct': yield_pct, 'dividend_annual': dps}}


def test_candidates_require_both_passes_and_annual_threshold():
    items = [candidate(), candidate('600000', 6), candidate('600001', 4),
             candidate('600002', 3.9), candidate('600003', None),
             candidate('600004', 7), candidate('600005', 8)]
    items[-2]['status'] = 'pending'
    items[-1]['core']['status'] = 'fail'
    snapshot = {'items': items}
    original = copy.deepcopy(snapshot)
    rows = entry_candidates(snapshot, preferences())
    assert [row['code'] for row in rows] == ['600000', '600941']
    assert rows[0]['reference_price'] == 99.98
    assert snapshot == original
    assert entry_candidates(None, preferences()) == []


def test_missing_annual_dividend_does_not_use_ttm_or_zero():
    item = candidate(dps=None)
    item['metrics']['dividend_ttm'] = 10
    assert entry_candidates({'items': [item]}, preferences())[0]['reference_price'] is None
    assert entry_candidates({'items': [candidate()]}, None)[0]['reference_price'] is None


def test_missing_core_verification_is_not_a_candidate():
    item = candidate()
    item['core'] = None
    assert entry_candidates({'items': [item]}, preferences()) == []


@pytest.mark.parametrize('field,value', [
    ('capital', 0), ('capital', True), ('capital', float('nan')),
    ('capital', float('inf')), ('capital', []), ('target_yield', 3.99),
    ('target_yield', 31), ('position_pct', 101), ('horizon_years', 0),
])
def test_invalid_personal_limits(field, value):
    values = preferences()
    values[field] = value
    with pytest.raises(ValueError):
        validate_preferences(values)


def test_preferences_persist_without_changing_holdings_or_model_settings(tmp_path, monkeypatch):
    store = DesktopStore(tmp_path)
    api = DesktopAPI(store)
    monkeypatch.setattr(api, '_read_dividend_screen', lambda: None)
    try:
        original_holdings = store.holdings()
        assert api.entry_plan()['data']['preferences'] is None
        values = {key: str(value) for key, value in preferences().items()}
        assert api.save_entry_preferences(values)['ok']
        assert DesktopStore(tmp_path).setting('entry_preferences') == preferences()
        plan = api.entry_plan()['data']
        assert plan['preferences'] == preferences()
        assert plan['as_of'] is None and plan['generated_at'] is None
        assert plan['candidates'] == []
        assert plan['market']['status'] == 'unknown'
        assert store.holdings() == original_holdings
        assert store.setting('model') is None
        assert not api.save_entry_preferences({'capital': 0})['ok']
        assert store.setting('entry_preferences') == preferences()
    finally:
        api._tasks.close()
        api._dividends.close()


def strategy_snapshot():
    item = candidate()
    item['metrics']['price'] = 90
    environment = {'status': 'normal', 'as_of': '2026-11-06', 'reason': '测试'}
    return {'as_of': '2026-11-08', 'items': [item], 'market_context': {
        'as_of': '2026-11-08', 'market': environment.copy(),
        'sectors': {'测试': environment.copy()}}}


def plan(snapshot, holdings=None, prefs=None):
    return strategy_plan(snapshot, prefs or preferences(), holdings or [], date(2026, 11, 8))


def test_strategy_readiness_does_not_change_snapshot():
    snapshot = strategy_snapshot()
    original = copy.deepcopy(snapshot)
    result = plan(snapshot)
    assert result['candidates'][0]['strategy']['status'] == 'research'
    assert result['portfolio']['status'] == 'empty'
    assert snapshot == original
    snapshot['items'][0]['metrics']['price'] = 100
    assert plan(snapshot)['candidates'][0]['strategy']['status'] == 'price'


@pytest.mark.parametrize('field', ['market', 'sector'])
def test_strategy_waits_for_market_or_sector_risk(field):
    snapshot = strategy_snapshot()
    env = snapshot['market_context']
    (env['market'] if field == 'market' else env['sectors']['测试'])['status'] = 'caution'
    assert plan(snapshot)['candidates'][0]['strategy']['status'] == 'wait'


@pytest.mark.parametrize('change', ['missing', 'stale', 'future', 'mismatch'])
def test_strategy_missing_or_invalid_dates_are_not_normal(change):
    snapshot = strategy_snapshot()
    if change == 'missing':
        snapshot.pop('market_context')
    elif change == 'stale':
        snapshot['as_of'] = '2026-10-01'
    elif change == 'future':
        snapshot['market_context']['market']['as_of'] = '2026-11-08'
    else:
        snapshot['market_context']['as_of'] = '2026-11-07'
    assert plan(snapshot)['candidates'][0]['strategy']['status'] == 'data'


def test_portfolio_uses_market_value_and_rejects_partial_coverage():
    snapshot = strategy_snapshot()
    second = candidate('600000')
    second['sector'] = '另一行业'
    second['metrics']['price'] = 10
    snapshot['items'].append(second)
    holdings = [{'ticker': '600941.SS', 'quantity': 10, 'cost': 1},
                {'ticker': '600000.SS', 'quantity': 10, 'cost': 999}]
    result = plan(snapshot, holdings)
    assert result['portfolio']['sectors'][0] == {'sector': '测试', 'weight_pct': 90}
    row = next(row for row in result['candidates'] if row['code'] == '600941')
    assert row['strategy']['held_sector_pct'] == 90
    holdings.append({'ticker': '000001.SZ', 'quantity': 1, 'cost': 100})
    result = plan(snapshot, holdings)
    assert result['portfolio']['sectors'] == []
    assert result['portfolio']['missing_codes'] == ['000001']
    assert result['candidates'][0]['strategy']['status'] == 'data'
