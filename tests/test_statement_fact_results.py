"""Statement tools expose the original measurements without reparsing rendered CSV."""
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.dataflows import date_window
from tradingagents.dataflows.vendors.akshare import common, fundamentals


def test_statement_result_fetches_once_and_keeps_period_and_currency(monkeypatch):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: '2026-11-05')
    request = Mock(return_value=pd.DataFrame([
        {'REPORT_DATE': '2026-09-30', 'NOTICE_DATE': '2026-10-21', 'CURRENCY': 'CNY',
         'NETCASH_OPERATE': 185290000000, 'NETCASH_INVEST': -89505000000, 'NETCASH_FINANCE': -94394000000},
    ]))
    monkeypatch.setattr(common, 'request', request)
    result = fundamentals.get_cashflow_result('600941.SH', 'quarterly', '2026-11-05')
    request.assert_called_once()
    fact = next(f for f in result.facts if f.key == 'NETCASH_OPERATE.reported')
    assert fact.value == 185290 and fact.unit == '百万元' and fact.currency == 'CNY'
    assert fact.period == '2026-09-30' and fact.basis == 'year-to-date cumulative'
    rejected = [f for f in result.facts if f.key.startswith('NETCASH_OPERATE.') and f.status != 'available']
    assert rejected and all(f.value is None for f in rejected)


def test_historical_result_has_no_current_facts(monkeypatch):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: '2026-11-05')
    request = Mock()
    monkeypatch.setattr(common, 'request', request)
    result = fundamentals.get_balance_sheet_result('600941.SH', as_of_date='2026-11-04')
    assert 'withheld' in result.content and result.facts == ()
    request.assert_not_called()


@pytest.mark.parametrize('metadata', [{'CURRENCY': pd.NA}, {'CURRENCY': 'CNY', 'UNIT': pd.NA}, {'CURRENCY': 'CNY', 'UNIT': ''}])
def test_unknown_source_currency_or_explicit_unit_cannot_become_a_verified_amount(monkeypatch, metadata):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: '2026-11-05')
    monkeypatch.setattr(common, 'request', lambda *a, **kw: pd.DataFrame([
        {'REPORT_DATE': '2026-09-30', 'NOTICE_DATE': '2026-10-21',
         'TOTAL_ASSETS': 100, 'TOTAL_LIABILITIES': 50, **metadata},
    ]))
    result = fundamentals.get_balance_sheet_result('600941.SH', as_of_date='2026-11-05')
    reported = next(f for f in result.facts if f.key == 'TOTAL_ASSETS.reported')
    assert reported.status == 'missing' and reported.value is None
