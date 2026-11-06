"""AKShare optional SDK access and package-level vendor routing."""
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.dataflows import date_window, router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError
from tradingagents.dataflows.vendors.akshare import common

TODAY = '2026-11-05'


@pytest.fixture(autouse=True)
def current_date(monkeypatch):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: TODAY)


def test_missing_sdk_is_configuration_failure(monkeypatch):
    monkeypatch.setattr(common.importlib, 'import_module', Mock(side_effect=ImportError('akshare')))
    with pytest.raises(VendorNotConfiguredError, match='a-shares'):
        common.request('stock_news_em', symbol='600941')


def test_sdk_request_failure_is_not_no_data(monkeypatch):
    sdk = Mock()
    sdk.stock_news_em.side_effect = TimeoutError('upstream timeout')
    monkeypatch.setattr(common.importlib, 'import_module', lambda _: sdk)
    with pytest.raises(VendorUnavailableError):
        common.request('stock_news_em', symbol='600941')


def test_default_router_prefers_akshare_for_a_share_statements(monkeypatch):
    monkeypatch.setattr(common, 'request', lambda *a, **k: pd.DataFrame([{'REPORT_DATE': '2026-09-30', 'NOTICE_DATE': '2026-10-21', 'CURRENCY': 'CNY', 'TOTAL_ASSETS': 100, 'TOTAL_LIABILITIES': 50}]))
    other = Mock(side_effect=AssertionError('unneeded fallback'))
    monkeypatch.setitem(router.VENDOR_METHODS['get_balance_sheet'], 'yfinance', other)
    monkeypatch.setitem(router.VENDOR_METHODS['get_balance_sheet'], 'sec_edgar', other)
    out = router.route_to_vendor('get_balance_sheet', '600941.SS', 'quarterly', TODAY)
    assert 'AKShare' in out
    other.assert_not_called()


def test_explicit_vendor_override_is_respected(monkeypatch):
    fetch = Mock(side_effect=AssertionError('AKShare was not selected'))
    monkeypatch.setattr(common, 'request', fetch)
    monkeypatch.setitem(router.VENDOR_METHODS['get_balance_sheet'], 'yfinance', lambda *a: 'YAHOO')
    with run_config({'tool_vendors': {'get_balance_sheet': 'yfinance'}}):
        assert router.route_to_vendor('get_balance_sheet', '600941.SS', 'quarterly', TODAY) == 'YAHOO'
    fetch.assert_not_called()
