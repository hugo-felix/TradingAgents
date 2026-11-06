"""Typed facts retain source units and dates without a second retrieval."""
from decimal import Decimal
from unittest.mock import Mock

import pandas as pd

from tradingagents.dataflows import date_window, market_session
from tradingagents.dataflows.vendors.akshare import common, dividends, valuation
from tradingagents.dataflows.vendors.yahoo import snapshot


def clock(monkeypatch):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: '2026-11-05')
    monkeypatch.setattr(market_session, '_now', lambda: pd.Timestamp('2026-11-05 16:00', tz='Asia/Shanghai'))


def test_snapshot_raw_precision_adjustment_and_volume_comparison(monkeypatch):
    clock(monkeypatch)
    frame = pd.DataFrame({'Date': pd.bdate_range('2026-10-26', periods=9),
                          'Open': 90.123456789, 'High': 92, 'Low': 89,
                          'Close': 91, 'Volume': [100] * 8 + [150]})
    fetch = Mock(return_value=frame)
    monkeypatch.setattr(snapshot, 'load_ohlcv', fetch)
    result = snapshot.build_verified_market_snapshot_result('600941.SS', '2026-11-05', indicators=['rsi'])
    fetch.assert_called_once()
    facts = {f.key: f for f in result.facts}
    opening = next(f for f in facts.values() if f.label == 'Open')
    assert opening.value == Decimal('90.123456789')
    assert 'auto_adjust=True' in ' '.join(opening.qualifiers)
    assert opening.period == '2026-11-05'
    volumes = [f for f in facts.values() if f.basis == 'daily volume']
    assert len(volumes) == 8 and volumes[-1].value == 150
    ratio = next(f for f in facts.values() if f.basis == 'latest volume / preceding 7 available sessions mean')
    assert ratio.value == Decimal('1.5')


def test_dividend_fact_is_gross_per_share_cny_with_scheme_dates(monkeypatch):
    clock(monkeypatch)
    fetch = Mock(return_value=pd.DataFrame([{'报告期': '2026-06-30', '最新公告日期': '2026-08-28',
        '除权除息日': '2026-09-07', '方案进度': '实施分配', '现金分红-现金分红比例': '25.1'}]))
    monkeypatch.setattr(common, 'request', fetch)
    result = dividends.get_dividends_result('600941.SS', '2026-11-05')
    fetch.assert_called_once()
    fact = result.facts[0]
    assert fact.value == Decimal('2.51') and fact.currency == 'CNY' and fact.unit == 'CNY/share'
    assert fact.period == '2026-06-30'
    assert '2026-09-07' in ' '.join(fact.qualifiers) and '实施分配' in ' '.join(fact.qualifiers)
    assert 'constant-currency' in ' '.join(fact.qualifiers)


def test_valuation_fact_missing_latest_not_backfilled_and_metric_specific_counts(monkeypatch):
    clock(monkeypatch)
    fetch = Mock(return_value=pd.DataFrame({'数据日期': ['2026-11-04', '2026-11-05'],
                                          'PE(TTM)': [10, None], '市净率': [1, 2]}))
    monkeypatch.setattr(common, 'request', fetch)
    result = valuation.get_valuation_comparison_result('600000.SH', '2026-11-05')
    fetch.assert_called_once()
    pe = next(f for f in result.facts if f.label == 'PE(TTM)' and f.basis == 'current self valuation')
    assert pe.value is None and pe.status == 'missing' and pe.period == '2026-11-05'
    counts = {f.label: f.value for f in result.facts if f.basis == 'positive finite self history sample count'}
    assert counts == {'PE(TTM)': Decimal(1), 'PB': Decimal(2)}
    percentile = next(f for f in result.facts if f.label == 'PB' and f.unit == '%')
    assert percentile.value == 100 and 'not fair value' in ' '.join(percentile.qualifiers)


def test_mobile_results_iot_stock_vs_flows_and_official_actuals(monkeypatch):
    from datetime import date

    from tradingagents.dataflows.vendors import china_mobile as mobile
    monkeypatch.setattr(mobile, '_today', lambda: date(2026, 11, 5))
    pdf = Mock(side_effect=lambda client, url: [
        'CHINA MOBILE LIMITED INTERIM REPORT 2026',
        'In the first half of 2026, our capital expenditure was RMB61.0 billion. Mobile ARPU was RMB45.1.'
    ] if '2026' in url else [])
    monkeypatch.setattr(mobile, '_official_pdf_pages', pdf)
    monkeypatch.setattr(mobile.cninfo, '_announcements', lambda *a, **kw: ([{
        'title': '2026年第三季度客户数据公告', 'day': date(2026, 10, 21), 'url': 'https://static.cninfo.com.cn/a.pdf'}], False))
    customer_pdf = Mock(return_value=['中国移动有限公司 600941 2026年第三季度客户数据公告\n单位：万户\n'
        '移动客户数 101,405.5\n本季度净增客户数 300.9\n本年累计净增客户数 888.6\n'
        '物联网卡连接数 149,686.2\n本季度净增连接数 -1,454.6\n本年累计净增连接数 1,503.2'])
    monkeypatch.setattr(mobile.cninfo, '_pdf_pages', customer_pdf)
    result = mobile.get_company_operating_history_result('600941.SS')
    assert pdf.call_count == 3 and customer_pdf.call_count == 1
    iot = [f for f in result.facts if '物联网' in f.label]
    assert {(f.value, f.basis) for f in iot} == {
        (Decimal('149686.2'), 'period-end connections'),
        (Decimal('-1454.6'), 'quarterly net additions'),
        (Decimal('1503.2'), 'year-to-date net additions')}
    assert all(f.unit == '万户' and f.period == '2026Q3' for f in iot)
    capex = next(f for f in result.facts if f.label == '实际资本开支')
    assert capex.value == Decimal('61.0') and capex.period == '2026H1' and capex.unit == 'RMB billion'
    plan = next(f for f in result.facts if f.label == '全年资本开支计划')
    assert plan.value is None and plan.status == 'unverified'
    assert 'publication_date=unknown' in ' '.join(capex.qualifiers)


def test_cninfo_currency_growth_facts_are_dynamic_and_from_one_pdf_read(monkeypatch):
    from datetime import date

    from tradingagents.dataflows.vendors import cninfo
    row = {'title': '2027年中期利润分派A股实施公告', 'day': date(2027, 9, 1),
           'url': 'https://static.cninfo.com.cn/a.pdf'}
    monkeypatch.setattr(cninfo, '_announcements', lambda *a, **kw: ([row], False))
    fetch = Mock(return_value=['中国移动有限公司 600941 公司决定向全体股东派发截至2027年6月30日止六个月之中期股息每股2.63',
        '元人民币（含税），同比增长1.2%。股息以人民币计值及宣派，并以港元或人民币支付。港元股息金额为每股3.01港元（含税），同比增长4.7%。'])
    monkeypatch.setattr(cninfo, '_pdf_pages', fetch)
    result = cninfo.get_company_disclosures_result('600941.SS', '2027-11-05')
    fetch.assert_called_once()
    growth = [f for f in result.facts if f.unit == '%']
    assert {(f.currency, f.value) for f in growth} == {('CNY', Decimal('1.2')), ('HKD', Decimal('4.7'))}
    assert all(f.period == '2027H1' for f in growth)
    assert all('currency' in ' '.join(f.qualifiers).lower() for f in growth)
    assert {f.value for f in result.facts if f.unit.endswith('/share')} == {Decimal('2.63'), Decimal('3.01')}


def test_snapshot_macd_has_price_units_and_legacy_volume_keeps_integer_display(monkeypatch):
    clock(monkeypatch)
    frame = pd.DataFrame({'Date': pd.bdate_range('2026-10-26', periods=9),
                          'Open': 90, 'High': 92, 'Low': 89, 'Close': range(90, 99), 'Volume': 100})
    monkeypatch.setattr(snapshot, 'load_ohlcv', lambda *a, **kw: frame)
    result = snapshot.build_verified_market_snapshot_result('600941.SS', '2026-11-05', indicators=['macd'])
    macd = next(f for f in result.facts if f.label == 'macd')
    assert macd.unit == 'price' and macd.currency == 'CNY'
    assert '| Volume | 100 |' in result.content


def test_cninfo_wrong_issuer_does_not_create_verified_dividend_facts():
    from tradingagents.dataflows.vendors.cninfo import _mobile_dividend_facts
    row = {'title': '2028年中期利润分派A股实施公告', 'url': 'https://static.cninfo.com.cn/a.pdf'}
    pages = ['Other issuer 600000 截至2028年6月30日止六个月之中期股息每股3.0元人民币（含税），同比增长2.0%。']
    assert _mobile_dividend_facts(row, pages) == []
