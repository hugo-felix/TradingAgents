"""Domestic macro series must never silently answer a US indicator request."""
import importlib
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.agents import tools
from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.vendors.akshare import common

TODAY = '2026-11-05'


@pytest.fixture
def vendor(monkeypatch):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: TODAY)
    return importlib.import_module('tradingagents.dataflows.vendors.akshare.macro')


@pytest.mark.parametrize('indicator', ['cpi', 'fed_rate', 'GDP', 'china_unknown', 'unemployment'])
def test_non_supported_identifiers_rejected_before_sdk(vendor, monkeypatch, indicator):
    fetch = Mock()
    monkeypatch.setattr(common, 'request', fetch)
    with pytest.raises(ValueError):
        vendor.get_macro_data(indicator, TODAY)
    fetch.assert_not_called()


@pytest.mark.parametrize('indicator,endpoint,rows,expected,excluded', [
    ('china_cpi', 'macro_china_cpi', [{'月份': '2026年09月份', '全国-同比增长': .9, '全国-环比增长': .2, '全国-当月': 100.9}], 'CPI YoY (%)', '100.9'),
    ('china_pmi', 'macro_china_pmi', [{'月份': '2026年10月份', '制造业-指数': 49.3, '非制造业-指数': 49.7, '制造业-同比增长': 99999}], 'Manufacturing PMI (index)', '99999'),
    ('china_gdp', 'macro_china_gdp', [{'季度': '2026年第1-3季度', '国内生产总值-绝对值': 1071071.2, '国内生产总值-同比增长': 4.6}], '2026年第1-3季度', 'standalone quarterly GDP'),
    ('china_lpr', 'macro_china_lpr', [{'TRADE_DATE': '2026-10-20', 'LPR1Y': 3., 'LPR5Y': 3.5, 'RATE_1': 99999}], 'LPR 1Y (%)', '99999'),
    ('china_credit', 'macro_china_new_financial_credit', [{'月份': '2026年09月份', '当月': 12734., '累计': 114995.}], 'Credit YTD (CNY 100 million)', '同比'),
])
def test_selected_fields_units_and_source(vendor, monkeypatch, indicator, endpoint, rows, expected, excluded):
    fetch = Mock(return_value=pd.DataFrame(rows))
    monkeypatch.setattr(common, 'request', fetch)
    out = vendor.get_macro_data(indicator, TODAY, 365)
    fetch.assert_called_once_with(endpoint)
    assert endpoint in out and expected in out and excluded not in out
    assert 'fetched at' in out and 'release' in out and 'vintage' in out
    if indicator == 'china_gdp':
        assert 'cumulative' in out and 'CNY 100 million' in out and '2026-09-30' in out


@pytest.mark.parametrize('indicator', ['china_cpi', 'china_pmi', 'china_gdp', 'china_lpr', 'china_credit'])
def test_historical_series_withheld_without_fetch(vendor, monkeypatch, indicator):
    fetch = Mock()
    monkeypatch.setattr(common, 'request', fetch)
    out = vendor.get_macro_data(indicator, '2026-10-01')
    assert 'withheld' in out and 'vintage' in out
    fetch.assert_not_called()


def test_window_future_rows_and_bad_dates(vendor, monkeypatch):
    rows = [{'月份': month, '全国-同比增长': 1., '全国-环比增长': .2}
            for month in ['2026年10月份', '2026年09月份', '2026年11月份', 'bad date']]
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame(rows))
    out = vendor.get_macro_data('china_cpi', TODAY, 10)
    assert '2026年10月份' in out and '2026年09月份' not in out and '2026年11月份' not in out
    assert 'bad date' not in out


@pytest.mark.parametrize('days', [-1, 0, 1.5, True])
def test_invalid_window_rejected_without_fetch(vendor, monkeypatch, days):
    fetch = Mock()
    monkeypatch.setattr(common, 'request', fetch)
    with pytest.raises(ValueError):
        vendor.get_macro_data('china_cpi', TODAY, days)
    fetch.assert_not_called()


def test_empty_window_is_coverage_gap(vendor, monkeypatch):
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame())
    assert 'coverage' in vendor.get_macro_data('china_cpi', TODAY).lower()


def test_schema_drift_not_silently_accepted(vendor, monkeypatch):
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame([{'月份': '2026年10月份'}]))
    with pytest.raises(VendorUnavailableError):
        vendor.get_macro_data('china_cpi', TODAY)


def test_row_limit(vendor, monkeypatch):
    rows = [{'TRADE_DATE': d, 'LPR1Y': 3., 'LPR5Y': 3.5} for d in pd.date_range('2026-01-01', periods=100)]
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame(rows))
    out = vendor.get_macro_data('china_lpr', TODAY)
    csv_lines = [line for line in out.splitlines() if line and not line.startswith('#')]
    assert len(csv_lines) <= 25


def test_default_window_matches_the_public_tools_one_year_contract(vendor, monkeypatch):
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame([
        {'TRADE_DATE': '2026-10-20', 'LPR1Y': 3., 'LPR5Y': 3.5},
        {'TRADE_DATE': '2024-10-20', 'LPR1Y': 99999., 'LPR5Y': 99999.},
    ]))
    out = vendor.get_macro_data('china_lpr', TODAY)
    assert '2026-10-20' in out and '2024-10-20' not in out and '99999' not in out


def test_china_macro_alias_uses_akshare_without_querying_fred(monkeypatch):
    import pandas as pd

    from tradingagents.dataflows import date_window, router
    from tradingagents.dataflows.vendors.akshare import common

    monkeypatch.setattr(date_window, 'get_current_date', lambda: '2026-11-05')
    fetch = Mock(return_value=pd.DataFrame({'月份': ['2026年09月份'], '全国-同比增长': [.9], '全国-环比增长': [.2]}))
    monkeypatch.setattr(common, 'request', fetch)
    foreign = Mock(side_effect=AssertionError('Do not substitute overseas CPI'))
    monkeypatch.setitem(router.VENDOR_METHODS['get_macro_indicators'], 'fred', foreign)
    out = tools.get_macro_indicators.func('china_cpi', '2026-11-05', trade_date='2026-11-05')
    assert 'China macro: china_cpi' in out and '0.9' in out
    fetch.assert_called_once_with('macro_china_cpi')
    foreign.assert_not_called()


def test_unprefixed_macro_keeps_its_existing_fred_meaning(monkeypatch):
    from tradingagents.dataflows import router
    from tradingagents.dataflows.vendors.akshare import common

    fetch = Mock(side_effect=AssertionError('No China request for US CPI'))
    monkeypatch.setattr(common, 'request', fetch)
    foreign = Mock(return_value='US CPI from FRED')
    monkeypatch.setitem(router.VENDOR_METHODS['get_macro_indicators'], 'fred', foreign)
    assert tools.get_macro_indicators.func('cpi', '2026-11-05', trade_date='2026-11-05') == 'US CPI from FRED'
    foreign.assert_called_once()
    fetch.assert_not_called()
