"""AKShare statements preserve disclosure dates, accounting fields and reporting bases."""
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.akshare import common, fundamentals as vendor

TODAY = '2026-11-05'


@pytest.fixture(autouse=True)
def current_date(monkeypatch):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: TODAY)


def statement(**updates):
    row = {'REPORT_DATE': '2026-09-30', 'NOTICE_DATE': '2026-10-21',
           'SECURITY_NAME_ABBR': '中国移动', 'CURRENCY': 'CNY',
           'TOTAL_ASSETS': 2113824000000, 'TOTAL_LIABILITIES': 713358000000,
           'OPERATE_INCOME': 780295000000, 'PARENT_NETPROFIT': 106198000000,
           'NETCASH_OPERATE': 185290000000, 'NETCASH_INVEST': -89505000000,
           'NETCASH_FINANCE': -94394000000}
    return {**row, **updates}


@pytest.mark.parametrize('symbol,expected', [
    ('600941.SS', 'SH600941'), ('600941.SH', 'SH600941'),
    ('000001.SZ', 'SZ000001'), ('300750.SZ', 'SZ300750'), ('920001.BJ', 'BJ920001'),
])
def test_market_code_is_converted_before_request(monkeypatch, symbol, expected):
    fetch = Mock(return_value=pd.DataFrame([statement()]))
    monkeypatch.setattr(common, 'request', fetch)
    out = vendor.get_balance_sheet(symbol, 'quarterly', TODAY)
    fetch.assert_called_once_with('stock_balance_sheet_by_report_em', symbol=expected)
    assert 'Total Assets' in out and '2113824000000' in out


@pytest.mark.parametrize('symbol', ['AAPL', '0941.HK', '000001.SS', '399001.SZ', '899050.BJ', '600941.SZ', '600941', '../600941.SS'])
def test_non_a_shares_do_not_request_akshare(monkeypatch, symbol):
    fetch = Mock(side_effect=AssertionError('must not request'))
    monkeypatch.setattr(common, 'request', fetch)
    with pytest.raises(NoMarketDataError):
        vendor.get_balance_sheet(symbol, 'quarterly', TODAY)
    fetch.assert_not_called()


@pytest.mark.parametrize('function,field,expected', [
    ('get_balance_sheet', 'TOTAL_ASSETS', 'Total Assets'),
    ('get_income_statement', 'OPERATE_INCOME', 'Operating Revenue'),
    ('get_cashflow', 'NETCASH_OPERATE', 'Operating Cash Flow'),
])
def test_statements_are_dated_and_have_explicit_units(monkeypatch, function, field, expected):
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame([statement()]))
    out = getattr(vendor, function)('600941.SS', 'quarterly', TODAY)
    assert expected in out and '2026-09-30' in out and '2026-10-21' in out
    assert 'CNY' in out and 'year-to-date' in out and field not in out


def test_annual_selection_and_future_disclosures_are_filtered(monkeypatch):
    rows = [statement(), statement(REPORT_DATE='2025-12-31'),
            statement(REPORT_DATE='2026-12-31', NOTICE_DATE='2027-03-01')]
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame(rows))
    out = vendor.get_balance_sheet('600941.SS', 'annual', TODAY)
    assert '2025-12-31' in out and '2026-09-30' not in out and '2027-03-01' not in out


@pytest.mark.parametrize('function', ['get_balance_sheet', 'get_income_statement', 'get_cashflow'])
def test_historical_statements_do_not_expose_current_restatements(monkeypatch, function):
    fetch = Mock(side_effect=AssertionError('must not request'))
    monkeypatch.setattr(common, 'request', fetch)
    out = getattr(vendor, function)('600941.SS', 'quarterly', '2026-10-01')
    assert 'withheld' in out and 'revision' in out
    fetch.assert_not_called()


def test_financial_calculations_use_disclosed_same_metric_ytd(monkeypatch):
    rows = [statement(NETPROFIT=120000000000),
            statement(REPORT_DATE='2026-06-30', NOTICE_DATE='2026-08-21',
                      PARENT_NETPROFIT=78934000000, NETPROFIT=85000000000),
            statement(REPORT_DATE='2025-09-30', NOTICE_DATE='2025-10-21',
                      PARENT_NETPROFIT=100000000000),
            statement(REPORT_DATE='2026-12-31', NOTICE_DATE='2027-03-01')]
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame(rows))
    out = vendor.get_income_statement('600941.SS', 'quarterly', TODAY)
    assert 'Code-calculated comparisons' in out
    assert '2026-09-30,Parent Net Profit,standalone quarter,CNY/' in out
    assert '27264000000.000000' in out and '6.198000' in out
    assert 'not calculated' in out and '2027-03-01' not in out
    assert 'EPS must not be differenced' in out
    raw, calculated = out.split('# Code-calculated comparisons', 1)
    assert '2026-06-30' in raw and '2025-09-30' in raw
    assert '\n2026-06-30,' not in calculated and '\n2025-09-30,' not in calculated
    assert '2026-06-30' in calculated  # Kept as the latest quarter's comparison input.
    assert '6.198000' in calculated  # Prior-year row remains available to the algorithm.


def test_cashflow_labels_do_not_claim_capex_or_pure_dividends(monkeypatch):
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame([
        statement(CONSTRUCT_LONG_ASSET=10, ASSIGN_DIVIDEND_PORFIT=5)]))
    out = vendor.get_cashflow('600941.SS', 'quarterly', TODAY)
    assert 'Investing Cash Flow (net)' in out
    assert '购建长期资产支付现金' in out
    assert 'Dividends/Profit Distributions and Interest Paid' in out
    assert 'not company-disclosed capex' in out and 'cannot establish a pure dividend payout ratio' in out


@pytest.mark.parametrize('rows', [[], [statement(TOTAL_ASSETS=None, TOTAL_LIABILITIES=None)]])
def test_empty_or_nonnumeric_statement_is_not_a_report(monkeypatch, rows):
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame(rows))
    with pytest.raises(NoMarketDataError):
        vendor.get_balance_sheet('600941.SS', 'quarterly', TODAY)


def test_unknown_statement_schema_is_provider_failure(monkeypatch):
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame({'unexpected': [1]}))
    with pytest.raises(VendorUnavailableError):
        vendor.get_balance_sheet('600941.SS', 'quarterly', TODAY)
