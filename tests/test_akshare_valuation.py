"""Conservative current-vintage A-share valuation history and fixed peers."""
import importlib
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.dataflows import date_window, market_session
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors.akshare import common

TODAY = '2026-11-05'


def report(ticker='600941.SS', as_of_date=TODAY, look_back_years=3):
    module = importlib.import_module('tradingagents.dataflows.vendors.akshare.valuation')
    return module.get_valuation_comparison(ticker, as_of_date, look_back_years)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: TODAY)
    monkeypatch.setattr(market_session, '_now', lambda: pd.Timestamp(TODAY + ' 16:00', tz='Asia/Shanghai'))


def frame(*rows):
    return pd.DataFrame([{'数据日期': date, 'PE(TTM)': pe, '市净率': pb, 'PE(静)': 987654321}
                         for date, pe, pb in rows])


def test_history_percentiles_use_positive_unique_completed_dates(monkeypatch):
    fetch = Mock(return_value=frame(('2026-11-05', 20, 2), ('2026-11-04', 10, 1),
                                   ('2026-11-04', 10, 1), ('2026-11-03', 0, float('inf')),
                                   ('2026-11-06', 999, 999), ('2022-01-01', 1, 1),
                                   ('invalid', 1, 1)))
    monkeypatch.setattr(common, 'request', fetch)
    out = report('600000.SH')
    fetch.assert_called_once_with('stock_value_em', symbol='600000')
    assert 'PE(TTM),20,2,2026-11-04,2026-11-05,100' in out
    assert 'PB,2,2,2026-11-04,2026-11-05,100' in out
    assert '987654321' not in out
    # Provenance fetched_at is a wall-clock timestamp, not a historical observation.
    assert '\n2026-11-06,' not in out and '\n2022-01-01,' not in out
    assert 'no configured peers' in out and 'not fair value' in out
    assert 'fetched_at' in out and 'source_url' in out and 'data_date' in out
    assert len(out) <= 16000


@pytest.mark.parametrize('ticker', ['600941', 'AAPL', '0941.HK', '123456.SS'])
def test_invalid_instrument_never_requests(monkeypatch, ticker):
    fetch = Mock()
    monkeypatch.setattr(common, 'request', fetch)
    with pytest.raises(NoMarketDataError):
        report(ticker)
    fetch.assert_not_called()


@pytest.mark.parametrize('date', ['2026-11-06', 'bad', '2026-11-05T12:00:00', '2026-02-30'])
def test_invalid_or_future_date_never_requests(monkeypatch, date):
    fetch = Mock()
    monkeypatch.setattr(common, 'request', fetch)
    with pytest.raises(ValueError):
        report(as_of_date=date)
    fetch.assert_not_called()


@pytest.mark.parametrize('years', [0, 2, True, '3'])
def test_unsupported_window_never_requests(monkeypatch, years):
    fetch = Mock()
    monkeypatch.setattr(common, 'request', fetch)
    with pytest.raises(ValueError):
        report(look_back_years=years)
    fetch.assert_not_called()


def test_historical_revision_withheld_without_requests(monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(common, 'request', fetch)
    assert 'withheld' in report(as_of_date='2026-11-04')
    fetch.assert_not_called()


def test_current_missing_values_are_not_backfilled_and_peers_do_not_change_anchor(monkeypatch):
    def fetch(endpoint, symbol, **kwargs):
        if symbol == '600941':
            return frame(('2026-11-04', 10, 1), (TODAY, None, 2))
        if symbol == '601728':
            return frame(('2026-11-04', 30, 3))
        raise VendorUnavailableError('peer connection failed')
    monkeypatch.setattr(common, 'request', fetch)
    out = report()
    assert 'anchor_date=2026-11-05' in out
    assert 'PE(TTM),missing,1,2026-11-04,2026-11-04,missing' in out
    assert '601728.SS,中国电信,2026-11-05,missing,missing,missing: no same-day row' in out
    assert 'peer connection failed' in out
    assert '600941.SS,中国移动,2026-11-05,missing,2' in out


def test_before_close_excludes_today_even_with_sh_alias(monkeypatch):
    monkeypatch.setattr(market_session, '_now', lambda: pd.Timestamp(TODAY + ' 14:00', tz='Asia/Shanghai'))
    monkeypatch.setattr(common, 'request', lambda *a, **k: frame(('2026-11-04', 10, 1), (TODAY, 999, 999)))
    out = report('600000.SH')
    assert 'anchor_date=2026-11-04' in out and ',999' not in out


@pytest.mark.parametrize('data', [pd.DataFrame(), frame((TODAY, 0, None))])
def test_self_unavailable_cannot_be_replaced_by_available_peers(monkeypatch, data):
    monkeypatch.setattr(common, 'request', lambda endpoint, symbol: data if symbol == '600941' else frame((TODAY, 20, 2)))
    out = report()
    assert 'self_status=unavailable' in out


def test_peer_failure_preserves_valid_self(monkeypatch):
    def fetch(endpoint, symbol):
        if symbol != '600941':
            raise VendorUnavailableError('provider offline')
        return frame((TODAY, 20, 2))
    monkeypatch.setattr(common, 'request', fetch)
    out = report()
    assert 'self_status=available' in out and 'provider offline' in out


def test_missing_sdk_remains_typed(monkeypatch):
    monkeypatch.setattr(common, 'request', Mock(side_effect=VendorNotConfiguredError('SDK missing')))
    with pytest.raises(VendorNotConfiguredError):
        report()


def test_self_failure_is_explicit_unavailable(monkeypatch):
    monkeypatch.setattr(common, 'request', Mock(side_effect=VendorUnavailableError('network unavailable')))
    out = report()
    assert 'self_status=unavailable' in out and 'network unavailable' in out


def test_optional_crosscheck_only_uses_same_day_and_never_changes_em_percentiles(monkeypatch):
    def fetch(endpoint, symbol, **kwargs):
        if endpoint == 'stock_value_em':
            return frame(('2026-11-04', 10, 1), (TODAY, 20, 2))
        assert kwargs['period'] == '近三年'
        if kwargs['indicator'] == '市净率':
            return pd.DataFrame({'date': ['2026-11-04'], 'value': [1234]})
        return pd.DataFrame({'date': ['2026-11-01', TODAY], 'value': [999, 21]})
    fetch_mock = Mock(side_effect=fetch)
    monkeypatch.setattr(common, 'request', fetch_mock)
    with run_config({'a_share_valuation_cross_check': True}):
        out = report('600000.SS')
    assert fetch_mock.call_count == 3
    assert 'PE(TTM),20,2,2026-11-04,2026-11-05,100' in out
    assert 'PE(TTM),20,21,1' in out
    assert 'PB,2,missing,missing' in out
    assert '1234' not in out and 'calendar-day' in out


def test_crosscheck_failure_isolated(monkeypatch):
    def fetch(endpoint, **kwargs):
        if endpoint == 'stock_value_em':
            return frame((TODAY, 20, 2))
        raise VendorUnavailableError('crosscheck failed')
    monkeypatch.setattr(common, 'request', fetch)
    with run_config({'a_share_valuation_cross_check': True}):
        out = report('600000.SS')
    assert 'self_status=available' in out and 'crosscheck failed' in out


def test_per_metric_sample_ranges_and_negative_nonfinite_filter(monkeypatch):
    monkeypatch.setattr(common, 'request', lambda *a, **k: frame(
        ('2026-11-02', -1, 1), ('2026-11-03', float('inf'), 2),
        ('2026-11-04', 10, float('-inf')), (TODAY, 5, 2)))
    out = report('600000.SS')
    assert 'PE(TTM),5,2,2026-11-04,2026-11-05,50' in out
    assert 'PB,2,3,2026-11-02,2026-11-05,100' in out


def test_missing_date_schema_degrades_without_discarding_valid_self(monkeypatch):
    monkeypatch.setattr(common, 'request', lambda endpoint, symbol: (
        frame((TODAY, 10, 1)) if symbol == '600941' else pd.DataFrame({'PE(TTM)': [20]})))
    out = report()
    assert 'self_status=available' in out and 'missing 数据日期' in out


@pytest.mark.parametrize('years,period', [(1, '近一年'), (5, '近五年')])
def test_optional_crosscheck_window_mapping(monkeypatch, years, period):
    def fetch(endpoint, **kwargs):
        if endpoint == 'stock_value_em':
            return frame(('2024-11-05', 5, 1), (TODAY, 10, 2))
        assert kwargs['period'] == period
        return pd.DataFrame({'date': [TODAY], 'value': [10]})
    monkeypatch.setattr(common, 'request', fetch)
    with run_config({'a_share_valuation_cross_check': True}):
        out = report('600000.SS', look_back_years=years)
    assert f'PE(TTM),10,{1 if years == 1 else 2},' in out


def test_failed_crosscheck_keeps_request_provenance(monkeypatch):
    def fetch(endpoint, **kwargs):
        if endpoint == 'stock_value_em':
            return frame((TODAY, 10, 1))
        raise VendorUnavailableError('baidu offline')
    monkeypatch.setattr(common, 'request', fetch)
    with run_config({'a_share_valuation_cross_check': True}):
        out = report('600000.SS')
    assert out.count('endpoint=stock_zh_valuation_baidu; source_url=') == 2


@pytest.mark.parametrize('self_data,expected', [
    (pd.DataFrame(), 'unavailable'),
    (frame(('2026-11-04', 10, 1), (TODAY, None, None)), 'unavailable'),
    (frame((TODAY, None, 2)), 'available'),
])
def test_collect_tool_evidence_reflects_current_self_coverage(monkeypatch, self_data, expected):
    from langchain_core.messages import ToolMessage

    from tradingagents.agents.evidence import collect_tool_evidence

    monkeypatch.setattr(common, 'request', lambda endpoint, symbol: (
        self_data if symbol == '600941' else frame((TODAY, 20, 2))))
    out = report()
    evidence = collect_tool_evidence('fundamentals', [ToolMessage(
        out, tool_call_id='valuation', name='get_valuation_comparison')])
    row = next(iter(evidence.values()))
    assert row['status'] == expected
    if expected == 'unavailable':
        assert out.startswith('DATA_UNAVAILABLE:')
        assert row['content'].startswith('DATA_UNAVAILABLE:')
    assert len(out) <= 16000 and row['content'] == out


def test_report_budget_preserves_missing_markers_under_long_provider_errors(monkeypatch):
    from langchain_core.messages import ToolMessage

    from tradingagents.agents.evidence import collect_tool_evidence

    def fetch(endpoint, symbol, **kwargs):
        if endpoint == 'stock_value_em' and symbol == '600941':
            return frame(*[(f'2026-10-{day:02}', 10, 1) for day in range(1, 29)], (TODAY, None, None))
        raise VendorUnavailableError('provider failure ' + 'x' * 30000)
    monkeypatch.setattr(common, 'request', fetch)
    with run_config({'a_share_valuation_cross_check': True}):
        out = report()
    evidence = collect_tool_evidence('fundamentals', [ToolMessage(
        out, tool_call_id='valuation', name='get_valuation_comparison')])
    row = next(iter(evidence.values()))
    assert len(out) <= 16000
    assert row['status'] == 'unavailable' and row['content'] == out
    assert 'self_status=unavailable' in out and out.count('missing:') >= 4
