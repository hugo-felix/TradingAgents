"""Synthetic disclosures: supplements must be dated, reversible and scope-specific."""
import copy
import json
from datetime import date

import pytest

from tradingagents.dataflows.dividend_financials import financial_checks
from tradingagents.dataflows.dividend_official import apply_evidence, listing_check, load_evidence
from tradingagents.dataflows.dividend_screen import annual_snapshot

ASOF = date(2026, 11, 7)


def evidence(**overrides):
    return {'code': '000001', 'entity': '虚构上市主体', 'scope': 'listed_entity',
            'report_date': '2026-09-30', 'notice_date': '2026-10-30',
            'source_url': 'https://example.org/official.pdf', 'page': 3, 'unit': '%',
            'fields': {'HXYJBCZL': 8.5, 'NEWCAPITALADER': 14}, **overrides}


def bank():
    m, checks = financial_checks([{'REPORT_DATE': '2026-09-30', 'NOTICE_DATE': '2026-10-30',
                                   'NONPERLOAN': 1, 'BLDKBBL': 200}], '银行', ASOF)
    return {'code': '000001', 'name': '测试银行', 'sector': '银行', 'metrics': m,
            'checks': checks, 'status': 'pending', 'sources': {}}


def test_missing_bank_ratio_supplement_can_fail_and_is_reversible():
    row = bank()
    apply_evidence(row, [evidence()], ASOF)
    assert row['status'] == 'fail'
    assert row['metrics']['financial_indicator_fields']['HXYJBCZL']['value'] == 8.5
    assert '第3页' in row['checks'][2]['detail']
    first = copy.deepcopy(row)
    apply_evidence(row, [evidence()], ASOF)
    assert row == first
    apply_evidence(row, [], ASOF)
    assert row['status'] == 'pending'
    assert row['metrics']['financial_indicator_fields']['HXYJBCZL']['value'] is None
    assert not row['sources']


def test_old_period_cannot_backfill_bank_ratio():
    row = bank()
    apply_evidence(row, [evidence(report_date='2026-06-30')], ASOF)
    assert row['status'] == 'pending'


def test_conflicting_sources_do_not_choose_convenient_value():
    row = bank()
    apply_evidence(row, [evidence(), evidence(fields={'HXYJBCZL': 12})], ASOF)
    assert row['status'] == 'pending'
    assert '冲突' in row['checks'][2]['detail']


def test_future_and_stale_bank_disclosures_cannot_backfill():
    rows = [{'REPORT_DATE': '2026-09-30', 'NOTICE_DATE': '2026-10-30'}]
    m, _ = financial_checks(rows, '银行', ASOF,
                            [evidence(notice_date='2026-11-08')])
    assert m['financial_indicator_fields']['HXYJBCZL']['value'] is None
    rows = [{'REPORT_DATE': '2025-12-31', 'NOTICE_DATE': '2026-01-30'}]
    m, _ = financial_checks(rows, '银行', ASOF,
                            [evidence(report_date='2025-12-31')])
    assert m['financial_indicator_fields']['HXYJBCZL']['value'] is None


def test_api_and_official_conflict_stays_pending():
    m, checks = financial_checks([
        {'REPORT_DATE': '2026-09-30', 'NOTICE_DATE': '2026-10-30', 'HXYJBCZL': 12}],
        '银行', ASOF, [evidence()])
    assert m['financial_indicator_fields']['HXYJBCZL']['value'] is None
    assert checks[2]['status'] == 'pending'
    assert '冲突' in checks[2]['detail']


def test_insurance_capital_and_rating_do_not_certify_asset_quality():
    m, checks = financial_checks([{'REPORT_DATE': '2026-09-30', 'NOTICE_DATE': '2026-10-31'}],
                                '保险', ASOF)
    row = {'code': '000001', 'sector': '保险', 'metrics': m, 'checks': checks, 'sources': {}}
    records = [evidence(fields={'CORE_SOLVENCY': 110, 'COMPREHENSIVE_SOLVENCY': 170}),
               evidence(report_date='2026-06-30', fields={'RISK_RATING': 'BBB'})]
    apply_evidence(row, records, ASOF)
    by_label = {c['label']: c for c in row['checks']}
    assert by_label['保险核心偿付能力充足率≥50%']['status'] == 'pass'
    assert by_label['保险风险综合评级≥B类']['status'] == 'pass'
    assert '评级期间 2026-06-30' in by_label['保险风险综合评级≥B类']['detail']
    assert by_label['保险投资资产质量']['status'] == 'pending'
    assert row['status'] == 'pending'
    apply_evidence(row, [], ASOF)
    assert 'CORE_SOLVENCY' not in row['metrics']['financial_indicator_fields']


@pytest.mark.parametrize('overrides', [
    {'scope': 'subsidiary'}, {'unit': '元'}, {'fields': {'HXYJBCZL': float('nan')}},
    {'source_url': 'file:///tmp/a.pdf'}, {'notice_date': '2026-01-01'},
    {'fields': {'RISK_RATING': '很好'}}, {'page': 0},
])
def test_invalid_evidence_rejected_visibly(tmp_path, overrides):
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps({'schema_version': 1, 'records': [evidence(**overrides)]}))
    records, error = load_evidence(path, ASOF)
    assert not records and error


def test_future_disclosure_excluded(tmp_path):
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps({'schema_version': 1, 'records': [evidence(notice_date='2026-11-08')]}))
    assert load_evidence(path, ASOF) == ([], None)


def test_new_listing_is_insufficient_history_not_generic_pending(tmp_path):
    row = bank()
    record = evidence(fields={'A_LISTING_DATE': '2026-09-30'})
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps({'schema_version': 1, 'records': [record]}))
    data = {'as_of': ASOF.isoformat(), 'rules': '', 'items': [row]}
    projected = annual_snapshot(data, official_path=path)
    assert projected['items'][0]['core']['status'] == 'fail'
    assert projected['items'][0]['core']['checks'][0]['label'] == 'A股上市满3年'
    assert 'a_listing_date' not in data['items'][0]['metrics']
    assert listing_check({'a_listing_date': '2023-11-07'}, ASOF)[0]['status'] == 'pass'
