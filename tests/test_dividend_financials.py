from datetime import date

import pytest

from tradingagents.dataflows.dividend_financials import financial_checks

ASOF = date(2026, 11, 7)


def report(**fields):
    return {'REPORT_DATE': '2026-09-30', 'NOTICE_DATE': '2026-10-30',
            'NONPERLOAN': 1, 'BLDKBBL': 200, 'HXYJBCZL': 12,
            'NEWCAPITALADER': 16, 'RISK_COVERAGE': 200,
            'CAPITAL_LEVERAGE_RATIO': 15, 'LIQUIDITY_COVERAGE_RATIO': 200,
            'NET_FUNDING_RATIO': 150, **fields}


@pytest.mark.parametrize('sector', ['银行', '证券'])
def test_financial_sector_can_pass_complete_fresh_indicators(sector):
    metrics, checks = financial_checks([report()], sector, ASOF)
    assert all(c['status'] == 'pass' for c in checks)
    assert metrics['financial_report_date'] == '2026-09-30'
    assert all('公告 2026-10-30' in c['detail'] for c in checks)


@pytest.mark.parametrize(('sector', 'field', 'value'), [
    ('银行', 'NONPERLOAN', 2.01), ('银行', 'BLDKBBL', 149.9),
    ('银行', 'HXYJBCZL', 8.99), ('银行', 'NEWCAPITALADER', 11.99),
    ('证券', 'RISK_COVERAGE', 119.99), ('证券', 'CAPITAL_LEVERAGE_RATIO', 9.59),
    ('证券', 'LIQUIDITY_COVERAGE_RATIO', 119.99), ('证券', 'NET_FUNDING_RATIO', -1),
])
def test_regulatory_ratios_below_research_buffer_fail(sector, field, value):
    _, checks = financial_checks([report(**{field: value})], sector, ASOF)
    assert checks[-1]['status'] == 'fail'


@pytest.mark.parametrize('value', [None, '', float('nan'), float('inf')])
def test_missing_ratio_never_passes_or_becomes_zero(value):
    metrics, checks = financial_checks([report(NONPERLOAN=value)], '银行', ASOF)
    assert checks[-1]['status'] == 'pending'
    assert metrics['financial_indicator_fields']['NONPERLOAN']['value'] is None


@pytest.mark.parametrize('overrides', [
    {'NOTICE_DATE': '2026-11-08'}, {'NOTICE_DATE': None},
    {'NOTICE_DATE': '2026-09-29'}, {'UPDATE_DATE': '2026-11-08'},
    {'REPORT_DATE': '2025-12-31', 'NOTICE_DATE': '2026-03-01'},
])
def test_unavailable_future_or_stale_reports_stay_pending(overrides):
    _, checks = financial_checks([report(**overrides)], '银行', ASOF)
    assert checks[-1]['status'] == 'pending'


def test_no_mixing_periods_to_fill_missing_current_ratio():
    rows = [report(NONPERLOAN=None), report(REPORT_DATE='2026-06-30', NOTICE_DATE='2026-08-30')]
    metrics, checks = financial_checks(rows, '银行', ASOF)
    assert checks[-1]['status'] == 'pending'
    assert metrics['financial_indicator_fields']['NONPERLOAN']['value'] is None


def test_insurance_ambiguous_solvency_cannot_qualify():
    metrics, checks = financial_checks([report(SOLVENCY_AR=200)], '保险', ASOF)
    assert checks[-1]['status'] == 'pending'
    assert metrics['financial_indicator_fields']['SOLVENCY_AR']['value'] == 200
    assert any('核心/综合' in c['detail'] for c in checks)


def test_insurance_reference_has_its_own_period_and_never_qualifies():
    rows = [report(SOLVENCY_AR=None),
            report(REPORT_DATE='2026-06-30', NOTICE_DATE='2026-08-21', SOLVENCY_AR=198.1)]
    metrics, checks = financial_checks(rows, '保险', ASOF)
    assert metrics['financial_report_date'] == '2026-09-30'
    reference = metrics['financial_indicator_fields']['SOLVENCY_AR']
    assert reference['value'] == 198.1
    assert reference['report_date'] == '2026-06-30'
    assert checks[-1]['status'] == 'pending'
    assert checks[0]['required'] is False


def test_worker_maps_exchange_symbol_for_indicator_api(monkeypatch, capsys):
    from tradingagents.dataflows import dividend_screen as screen

    class Frame:
        def to_json(self, **kwargs):
            return '[]'

    calls = []

    def request(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return Frame()

    monkeypatch.setattr('tradingagents.dataflows.vendors.akshare.common.request', request)
    screen.worker(screen.INDICATORS, 'SH600036')
    assert calls == [(screen.INDICATORS, {'symbol': '600036.SH', 'indicator': '按报告期'})]
    assert capsys.readouterr().out.strip() == '[]'


def test_bank_evaluate_uses_indicators_without_ordinary_debt_rule():
    from tests.test_dividend_screen import inputs
    from tradingagents.dataflows.dividend_screen import evaluate

    data = inputs()
    data['indicators'] = [report()]
    row = evaluate('600036', '招商银行', '银行',
                   {'price': 10, 'market_cap_yi': 500, 'quote_time': '20261106150000'},
                   data, ASOF)
    assert row['status'] == 'pass'
    assert not any('负债率' in c['label'] for c in row['checks'])
