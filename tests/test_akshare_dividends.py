"""Verified dividend scheme fields, with conservative disclosure-time boundaries."""
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import akshare
from tradingagents.dataflows.vendors.akshare import common

TODAY = '2026-11-05'


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: TODAY)


def scheme(**updates):
    return {'报告期': '2026-06-30', '最新公告日期': '2026-08-28',
            '预案公告日': '2026-08-13', '股权登记日': '2026-09-04',
            '除权除息日': '2026-09-07', '方案进度': '实施分配',
            '现金分红-现金分红比例': 25.1, '现金分红-现金分红比例描述': '10派25.1元(含税)',
            '每股净资产': 999999, '现金分红-股息率': 999999, **updates}


def test_dividend_amounts_and_stages_exclude_unverified_valuation(monkeypatch):
    fetch = Mock(return_value=pd.DataFrame([scheme(), scheme(最新公告日期='2026-11-06', 报告期='2026-09-30')]))
    monkeypatch.setattr(common, 'request', fetch)
    out = akshare.get_dividends('600941.SS', TODAY)
    fetch.assert_called_once_with('stock_fhps_detail_em', symbol='600941')
    assert '2.51' in out and '2.5100000000000002' not in out
    assert 'CNY/share' in out and 'per 10 shares' in out
    assert all(value in out for value in ['2026-06-30', '2026-08-28', '2026-09-04', '2026-09-07', '实施分配'])
    assert '999999' not in out and '2026-09-30' not in out
    assert 'fetched at' in out


@pytest.mark.parametrize('symbol', ['AAPL', '0941.HK', '600941'])
def test_non_a_shares_do_not_request(monkeypatch, symbol):
    fetch = Mock()
    monkeypatch.setattr(common, 'request', fetch)
    with pytest.raises(NoMarketDataError):
        akshare.get_dividends(symbol, TODAY)
    fetch.assert_not_called()


def test_historical_schemes_withhold_current_revisions_without_request(monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(common, 'request', fetch)
    out = akshare.get_dividends('600941.SS', '2026-08-01')
    assert 'withheld' in out and 'revision' in out
    fetch.assert_not_called()


@pytest.mark.parametrize('rows', [[], [scheme(最新公告日期=None)], [scheme(最新公告日期='2026-11-06')]])
def test_missing_disclosed_rows_mean_coverage_gap_not_no_dividend(monkeypatch, rows):
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame(rows))
    out = akshare.get_dividends('600941.SS', TODAY)
    assert 'coverage' in out.lower() and 'does not establish' in out


def test_missing_amount_column_is_schema_failure(monkeypatch):
    row = scheme()
    del row['现金分红-现金分红比例']
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame([row]))
    with pytest.raises(VendorUnavailableError):
        akshare.get_dividends('600941.SS', TODAY)
