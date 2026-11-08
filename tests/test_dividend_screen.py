import json
from datetime import date

import pytest

from tradingagents.dataflows.dividend_screen import Source, annual, atomic_json, evaluate
from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore


def inputs():
    dividends = [{'报告期': f'{y}-12-31', '除权除息日': f'{y+1}-06-01',
                  '最新公告日期': f'{y+1}-05-01', '方案进度': '实施分配',
                  '现金分红-现金分红比例': 5, '送转股份-送转总比例': 0}
                 for y in range(2021, 2026)]
    statements = [{'REPORT_DATE': f'{y}-12-31', 'NOTICE_DATE': f'{y+1}-03-01', 'CURRENCY': 'CNY',
                   'NETCASH_OPERATE': 6e9, 'PARENT_NETPROFIT': 5e9, 'NETPROFIT': 5e9,
                   'TOTAL_ASSETS': 100e9, 'TOTAL_LIABILITIES': 40e9}
                  for y in range(2023, 2026)]
    return {'dividends': dividends, **dict.fromkeys(('cashflow', 'profit', 'balance'), statements)}


def result(data=None, **quote):
    return evaluate('600941', 'fixture', '通信',
                    {'price': 10, 'market_cap_yi': 500, 'quote_time': '20261106150000', **quote},
                    inputs() if data is None else data, date(2026, 11, 6))


def test_valid_screen_and_strict_yield_boundary():
    assert result()['status'] == 'pass'
    assert result(price=12.5)['status'] == 'fail'  # exactly 4% is excluded
    assert result(price=12.5)['metrics']['yield_pct'] == 4


def test_missing_financial_fields_never_passes():
    data = inputs()
    data['dividends'] = [{'renamed': 5}]
    row = result(data)
    assert row['status'] == 'pending'
    assert row['metrics']['yield_pct'] is None
    assert result(data={})['status'] == 'pending'


def test_future_proposals_and_duplicate_dividends_excluded():
    data = inputs()
    event = data['dividends'][-1]
    data['dividends'] += [dict(event), {**event, '方案进度': '董事会预案', '现金分红-现金分红比例': 100},
                          {**event, '除权除息日': '2026-12-01'}]
    assert result(data)['metrics']['dividend_ttm'] == .5


@pytest.mark.parametrize('timestamp', ['20261001150000', '20261107150000', '', None])
def test_stale_or_future_quote_is_pending(timestamp):
    row = result(quote_time=timestamp)
    assert row['status'] == 'pending'
    assert row['metrics']['yield_pct'] is None


def test_split_adjusted_and_cashflow_deterioration_fails():
    data = inputs()
    data['dividends'][-1]['送转股份-送转总比例'] = 2
    assert result(data)['metrics']['yield_pct'] == pytest.approx(5 / 1.2)
    data = inputs()
    data['cashflow'][-1]['NETCASH_OPERATE'] = 1e9
    assert result(data)['status'] == 'fail'


def test_bank_cannot_pass_without_regulatory_review():
    data = inputs()
    row = evaluate('600941', 'fixture', '银行', {'price': 10, 'market_cap_yi': 500,
                   'quote_time': '20261106150000'}, data, date(2026, 11, 6))
    assert row['status'] == 'pending'
    assert any('资本充足' in c['label'] for c in row['checks'])


@pytest.mark.parametrize('sector', ['保险', '证券', '多元金融'])
def test_special_finance_is_pending_instead_of_failing_ordinary_debt_rules(sector):
    data = inputs()
    data['balance'] = [{**r, 'TOTAL_LIABILITIES': 90e9} for r in data['balance']]
    data['cashflow'] = [{**r, 'NETCASH_OPERATE': -1e9} for r in data['cashflow']]
    row = evaluate('601318', 'fixture', sector,
                   {'price': 10, 'market_cap_yi': 500, 'quote_time': '20261106150000'},
                   data, date(2026, 11, 6))
    assert row['status'] == 'pending'
    assert row['metrics']['debt_ratio_pct'] == 90
    assert row['metrics']['cfo_yuan'] == [-1e9] * 3
    assert any(c['label'] == '金融行业专属指标' and c['status'] == 'pending'
               for c in row['checks'])
    assert not any('现金流' in c['label'] or '负债率' in c['label'] for c in row['checks'])
    # Dividend and valuation failures still apply to financial companies.
    data['dividends'] = [{**r, '现金分红-现金分红比例': 1} for r in data['dividends']]
    assert evaluate('601318', 'fixture', sector,
                    {'price': 10, 'market_cap_yi': 500, 'quote_time': '20261106150000'},
                    data, date(2026, 11, 6))['status'] == 'fail'


def test_future_financial_notice_is_excluded():
    assert annual([{'REPORT_DATE': '2025-12-31', 'NOTICE_DATE': '2026-12-01'}], date(2026, 11, 6)) == {}


@pytest.mark.parametrize(('field', 'value'), [
    ('现金分红-现金分红比例', None), ('现金分红-现金分红比例', -1),
    ('送转股份-送转总比例', None), ('除权除息日', '2026-06-99'),
    ('最新公告日期', None), ('报告期', 'unknown'),
])
def test_invalid_implemented_dividend_is_unknown_not_zero(field, value):
    data = inputs()
    data['dividends'][-1][field] = value
    row = result(data)
    assert row['status'] == 'pending'
    assert row['metrics']['dividend_ttm'] is None
    assert row['metrics']['yield_pct'] is None


@pytest.mark.parametrize('omit_field', [False, True])
def test_incomplete_old_dividend_does_not_hide_valid_recent_yield(omit_field):
    data = inputs()
    old = {**data['dividends'][0], '报告期': '1995-06-30',
           '除权除息日': '1995-12-04', '最新公告日期': '1995-11-29',
           '送转股份-送转总比例': 5, '现金分红-现金分红比例': None}
    if omit_field:
        old.pop('现金分红-现金分红比例')
    data['dividends'].append(old)
    row = result(data)
    assert row['status'] == 'pass'
    assert row['metrics']['yield_pct'] == 5
    assert row['metrics']['dividend_ttm'] == .5

    # Neither a recently paid old report nor an undated event can be ignored.
    old['除权除息日'] = '2026-06-01'
    assert result(data)['metrics']['yield_pct'] is None
    old['除权除息日'] = None
    assert result(data)['metrics']['yield_pct'] == 5
    old['最新公告日期'] = None
    assert result(data)['metrics']['yield_pct'] is None


def test_announced_split_proposal_does_not_invalidate_implemented_yield():
    data = inputs()
    data['dividends'].append({**data['dividends'][-1], '方案进度': '董事会预案',
                              '送转股份-送转总比例': 10})
    assert result(data)['metrics']['yield_pct'] == 5


@pytest.mark.parametrize(('report', 'ex', 'notice'), [
    ('1996-12-31', '1996-12-09', '1996-12-03'),
    ('2003-12-31', None, '2004-05-28'),
])
def test_dated_ancient_defects_do_not_contaminate_recent_dividends(report, ex, notice):
    data = inputs()
    old = {'报告期': report, '除权除息日': ex, '最新公告日期': notice,
           '方案进度': '实施分配', '现金分红-现金分红比例': None}
    data['dividends'].append(old)
    row = result(data)
    assert row['metrics']['yield_pct'] == 5
    assert len(row['metrics']['dividend_excluded_history']) == 1
    assert old not in row['dividend_events']
    # A recent notice can indicate a delayed payment; never discard it.
    old['最新公告日期'] = '2026-05-28'
    assert result(data)['metrics']['yield_pct'] is None


def test_differential_dividend_uses_minority_pretax_amount_and_preserves_original():
    from tradingagents.dataflows.dividend_screen import dividend_metrics

    data = inputs()
    data['dividends'][-1].update({
        '现金分红-现金分红比例': 4.2, '送转股份-送转总比例': None,
        '现金分红-现金分红比例描述':
            '10派4.20元(含税,扣税后3.78元)(中小股东10派4.921元;大股东10派3.906元)',
    })
    row = result(data)
    assert row['metrics']['yield_pct'] == pytest.approx(4.921)
    event = row['dividend_events'][-1]
    assert event['现金分红-现金分红比例'] == 4.921
    assert event['source_cash_per10'] == 4.2
    assert event['cash_parse_basis'] == 'explicit_minority_shareholder_pretax_per10'
    assert data['dividends'][-1]['现金分红-现金分红比例'] == 4.2
    # Reprojecting a saved event must not revert to the weighted/tax-after rate.
    metrics, _, _ = dividend_metrics(row['metrics'], row['dividend_events'], date(2026, 11, 6))
    assert metrics['yield_pct'] == row['metrics']['yield_pct']


@pytest.mark.parametrize('description', [
    '10派4.3元(含税)(中小股东10派4.921元;大股东10派3.906元)',
    '10派4.2元(含税)(中小股东方案待定)',
    '10派4.2元(含税)(中小股东10派4.921元;大股东10派3.906元)另送股',
])
def test_ambiguous_differential_dividend_is_not_weighted_rate(description):
    data = inputs()
    data['dividends'][-1].update({'现金分红-现金分红比例': 4.2,
                                 '送转股份-送转总比例': 0,
                                 '现金分红-现金分红比例描述': description})
    assert result(data)['metrics']['yield_pct'] is None


def test_dividend_fallback_converts_units_keeps_provenance_and_does_not_merge():
    from tradingagents.dataflows.dividend_screen import DIVIDENDS_FALLBACK, fetch_dividends
    from tradingagents.dataflows.errors import VendorUnavailableError

    calls = []

    class FakeSource:
        def fetch(self, endpoint, symbol):
            calls.append(endpoint)
            if endpoint != DIVIDENDS_FALLBACK:
                raise VendorUnavailableError('primary unavailable')
            return {'endpoint': endpoint, 'symbol': symbol, 'rows': [{
                '报告时间': '2025年报', '实施方案公告日期': '2026-05-01',
                '除权日': '2026-06-01', '派息比例': 5, '送股比例': 0, '转增比例': 0,
                '实施方案分红说明': '10派5元(含税)',
            }]}

    rows, provenance, errors = fetch_dividends(FakeSource(), '600941')
    assert len(calls) == 2 and errors == ['primary unavailable']
    assert provenance['endpoint'] == DIVIDENDS_FALLBACK
    assert rows[0]['报告期'] == '2025-12-31'
    assert rows[0]['现金分红-现金分红比例'] == 5
    assert rows[0]['送转股份-送转总比例'] == 0
    assert rows[0]['source_record']['报告时间'] == '2025年报'
    data = inputs()
    data['dividends'][-1] = rows[0]
    assert result(data)['metrics']['yield_pct'] == 5


def test_dividend_fallback_failure_stays_unknown_and_primary_success_skips_backup():
    from tradingagents.dataflows.dividend_screen import ENDPOINTS, fetch_dividends
    from tradingagents.dataflows.errors import VendorUnavailableError

    class FailedSource:
        def fetch(self, endpoint, symbol):
            raise VendorUnavailableError(endpoint)

    rows, provenance, errors = fetch_dividends(FailedSource(), '600941')
    assert rows == [] and provenance == {} and len(errors) == 2

    class PrimarySource:
        def fetch(self, endpoint, symbol):
            assert endpoint == ENDPOINTS['dividends']
            return {'endpoint': endpoint, 'rows': inputs()['dividends']}

    assert fetch_dividends(PrimarySource(), '600941')[2] == []


@pytest.mark.parametrize(('description', 'expected'), [
    ('10派5.00元(含税,扣税后4.50元)', 5),
    ('10派5元(含税)', 5),
    ('10转2派5元(含税)', None),
    ('10派6元(含税)', None),
    ('10派5元(含税)另送股', None),
    ('', None),
])
def test_null_split_needs_explicit_matching_cash_only_plan(description, expected):
    data = inputs()
    data['dividends'][-1].update({'送转股份-送转总比例': None,
                                 '现金分红-现金分红比例描述': description})
    assert result(data)['metrics']['yield_pct'] == expected


@pytest.mark.parametrize('notice', ['2026-00-01', '2026-02-30', '2025-01-01', None])
def test_invalid_or_premature_notice_cannot_establish_available_annual_report(notice):
    assert annual([{'REPORT_DATE': '2025-12-31', 'NOTICE_DATE': notice}], date(2026, 11, 6)) == {}


def test_unknown_currency_is_not_compared_with_yuan_market_cap():
    data = inputs()
    data['profit'][-1]['CURRENCY'] = 'USD'
    row = result(data)
    assert row['status'] == 'pending'
    assert row['metrics']['pe_annual'] is None


def test_subprocess_module_and_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace

    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs['timeout'] == 50
        return SimpleNamespace(stdout='[{"value":1}]')

    monkeypatch.setattr('subprocess.run', run)
    source = Source(tmp_path)
    assert source.fetch('stock_fhps_detail_em', '600941')['rows'] == [{'value': 1}]
    assert source.fetch('stock_fhps_detail_em', '600941')['rows'] == [{'value': 1}]
    assert len(commands) == 1
    assert commands[0][2] == 'tradingagents.dataflows.dividend_screen'
    next(tmp_path.glob('*.json')).write_text('{broken', encoding='utf-8')
    source.fetch('stock_fhps_detail_em', '600941')
    assert len(commands) == 2


def test_snapshot_api_empty_valid_and_corrupt(tmp_path):
    api = DesktopAPI(DesktopStore(tmp_path))
    assert api.dividend_screen() == {'ok': True, 'data': None}
    atomic_json(tmp_path / 'dividend-screen.json', snapshot([]))
    assert api.dividend_screen()['data']['items'] == []
    (tmp_path / 'dividend-screen.json').write_text(json.dumps([]), encoding='utf-8')
    assert api.dividend_screen()['ok'] is False


def snapshot(items):
    return {'schema_version': 1, 'items': items, 'generated_at': '2026-11-06T16:00:00+08:00',
            'as_of': '2026-11-06', 'require_qualifications': False,
            'coverage': 'test fixture', 'rules': 'test rules', 'limitations': 'test only'}


@pytest.mark.parametrize(('field', 'value'), [
    ('metrics', None), ('metrics', {'yield_pct': float('nan')}),
    ('metrics', {'yield_pct': '5%'}), ('metrics', {'pe_annual': True}),
    ('checks', None), ('checks', []), ('checks', [{}]),
    ('checks', [{'status': [], 'label': 'invalid', 'detail': '', 'required': True}]),
    ('status', 'unknown'), ('status', []), ('sources', []), ('errors', 'failed'),
])
def test_malformed_snapshot_rows_return_actionable_error(tmp_path, field, value):
    api = DesktopAPI(DesktopStore(tmp_path))
    row = result()
    row[field] = value
    (tmp_path / 'dividend-screen.json').write_text(json.dumps(snapshot([row])), encoding='utf-8')
    response = api.dividend_screen()
    assert not response['ok']
    assert '快照格式无效' in response['error']


def test_snapshot_metadata_and_valid_candidate(tmp_path):
    api = DesktopAPI(DesktopStore(tmp_path))
    payload = snapshot([result()])
    atomic_json(tmp_path / 'dividend-screen.json', payload)
    assert api.dividend_screen()['ok']
    payload['generated_at'] = 'not a date'
    atomic_json(tmp_path / 'dividend-screen.json', payload)
    assert not api.dividend_screen()['ok']


def test_ownership_and_rank_no_longer_affect_results():
    row = result()
    assert row['status'] == 'pass'
    assert not any('实际控制' in c['label'] or '前三' in c['label'] for c in row['checks'])


def test_roster_recovers_missing_quote_without_mutating_cache():
    from tradingagents.dataflows import dividend_screen as screen

    packet = {'rows': [{'代码': f'{i:06}', '名称': 'fixture', '总市值': 499e8}
                       for i in range(4000)]}

    class FakeSource:
        def fetch(self, *args):
            return packet

    roster = {'status': 'confirmed', 'names': {'601318': '中国平安'}, 'boards': []}
    candidates, manifest, _ = screen.discover_market(FakeSource(), roster=roster)
    assert candidates[0][:2] == ('601318', '中国平安')
    assert len(packet['rows']) == 4000
    assert manifest['roster']['missing_quote_codes'] == ['601318']
    assert manifest['unknown_cap'] == 1
    subset, _, _ = screen.discover_market(FakeSource(), ['601318'], roster=roster)
    assert subset == candidates


def test_roster_failure_is_explicit_not_full_coverage():
    from tradingagents.dataflows import dividend_screen as screen
    from tradingagents.dataflows.errors import VendorUnavailableError

    class FailedSource:
        def fetch(self, *args):
            raise VendorUnavailableError('offline')

    roster = screen.exchange_roster(FailedSource())
    assert roster['status'] == 'unconfirmed'
    assert roster['names'] == {}
    assert len(roster['boards']) == 4
    assert all(b['status'] == 'unavailable' for b in roster['boards'])


def test_runtime_check_validates_sdk_without_fetching(monkeypatch, capsys):
    import sys
    from types import SimpleNamespace

    from tradingagents.dataflows import dividend_screen

    def forbidden(*args, **kwargs):
        pytest.fail('Runtime check must not fetch data')

    sdk = SimpleNamespace(**dict.fromkeys([*dividend_screen.ENDPOINTS.values(), dividend_screen.DISCOVERY,
                                           dividend_screen.DISCOVERY_FALLBACK,
                                           *dividend_screen.ROSTER_ENDPOINTS,
                                           dividend_screen.HISTORY, dividend_screen.PROFILE,
                                           dividend_screen.INDICATORS,
                                           dividend_screen.DIVIDENDS_FALLBACK,
                                           dividend_screen.INDEX_HISTORY,
                                           dividend_screen.INDEX_FALLBACK,
                                           dividend_screen.SECTOR_HISTORY], forbidden))
    monkeypatch.setitem(sys.modules, 'akshare', sdk)
    monkeypatch.setattr(sys, 'argv', ['screen', '--check-runtime'])
    monkeypatch.setattr(dividend_screen, 'run', forbidden)
    dividend_screen.main()
    assert 'dividend-runtime-ready' in capsys.readouterr().out
    sdk.stock_fhps_detail_em = None
    with pytest.raises(RuntimeError, match='stock_fhps_detail_em'):
        dividend_screen.main()


def test_market_discovery_includes_new_codes_and_unknown_cap():
    from tradingagents.dataflows.dividend_screen import discover
    rows = [{'代码': '688999', '名称': '新候选', '总市值': 500e8},
            {'代码': '920001', '名称': '未知市值', '总市值': None},
            {'代码': '000001', '名称': '小市值', '总市值': 499e8}]
    candidates, manifest = discover(rows)
    assert [r[0] for r in candidates] == ['688999', '920001']
    assert manifest['scanned'] == 3 and manifest['unknown_cap'] == 1
    assert manifest['excluded_small_cap'] == 1
    with pytest.raises(Exception, match='重复'):
        discover(rows + rows[:1])
    with pytest.raises(ValueError, match='指定代码'):
        discover(rows, ['600000'])


@pytest.mark.parametrize('primary', ['unavailable', 'truncated', 'duplicate'])
def test_market_discovery_falls_back_and_includes_pingan(primary):
    from tradingagents.dataflows import dividend_screen as screen
    from tradingagents.dataflows.errors import VendorUnavailableError

    class FakeSource:
        def fetch(self, endpoint, symbol):
            if endpoint == screen.DISCOVERY:
                if primary == 'unavailable':
                    raise VendorUnavailableError('fixture unavailable')
                rows = [{'代码': '600000', '名称': 'fixture', '总市值': 600e8}]
                return {'rows': rows * (2 if primary == 'duplicate' else 1)}
            rows = [{'code': f'sz{i:06}', 'name': 'fixture', 'zsz': '499'}
                    for i in range(4000)]
            rows += [{'code': 'sh601318', 'name': '中国平安', 'zsz': '9868.66'},
                     {'code': 'bj920001', 'name': '未知市值', 'zsz': '-'}]
            return {'endpoint': endpoint, 'rows': rows}

    candidates, coverage, packet = screen.discover_market(FakeSource())
    assert [r[0] for r in candidates] == ['601318', '920001']
    assert coverage['scanned'] == 4002
    assert coverage['excluded_small_cap'] == 4000
    assert coverage['unknown_cap'] == 1
    assert coverage['fallback_used'] is True
    assert packet['endpoint'] == screen.DISCOVERY_FALLBACK


def test_market_discovery_rejects_two_incomplete_providers():
    from tradingagents.dataflows import dividend_screen as screen
    from tradingagents.dataflows.errors import VendorUnavailableError

    class FakeSource:
        def fetch(self, endpoint, symbol):
            return {'rows': []}

    with pytest.raises(VendorUnavailableError, match='旧快照未覆盖'):
        screen.discover_market(FakeSource())


def test_complete_primary_discovery_does_not_fetch_fallback():
    from tradingagents.dataflows import dividend_screen as screen

    class FakeSource:
        def fetch(self, endpoint, symbol):
            assert endpoint == screen.DISCOVERY
            return {'endpoint': endpoint, 'rows': [
                {'代码': f'{i:06}', '名称': 'fixture', '总市值': 500e8}
                for i in range(4000)]}

    candidates, coverage, packet = screen.discover_market(FakeSource())
    assert len(candidates) == 4000
    assert coverage['fallback_used'] is False
    assert packet['endpoint'] == screen.DISCOVERY


def test_direct_fetch_bypasses_proxy_for_both_discovery_providers(tmp_path, monkeypatch):
    from types import SimpleNamespace

    def run(command, **kwargs):
        hosts = kwargs['env']['NO_PROXY'].split(',')
        assert '.eastmoney.com' in hosts
        assert '.finance.qq.com' in hosts
        assert kwargs['env']['no_proxy'] == kwargs['env']['NO_PROXY']
        return SimpleNamespace(stdout='[{"value":1}]')

    monkeypatch.setattr('subprocess.run', run)
    Source(tmp_path, direct=True).fetch('stock_zh_a_spot_tx', 'all')


def risk_history():
    from datetime import timedelta
    rows = []
    day = date(2023, 10, 1)
    price = 100
    while day < date(2026, 11, 6):
        if day.weekday() < 5:
            price *= 1.01 if len(rows) % 2 == 0 else 0.99
            rows.append({'日期': day.isoformat(), '收盘': price, '成交额': 2e8})
        day += timedelta(days=1)
    return rows


def test_risk_windows_ignore_unfinished_bar_and_measure_real_returns():
    import math
    import statistics

    from tradingagents.dataflows.dividend_screen import price_risk
    rows = risk_history()
    baseline, notes = price_risk(rows, date(2026, 11, 6))
    metrics, _ = price_risk(rows + [{'日期': '2026-11-06', '收盘': 1}], date(2026, 11, 6))
    assert metrics == baseline
    assert not notes
    returns = [b['收盘'] / a['收盘'] - 1 for a, b in zip(rows, rows[1:], strict=False)
               if b['日期'] > '2025-11-06']
    assert metrics['volatility_1y_pct'] == pytest.approx(statistics.stdev(returns) * math.sqrt(252) * 100)
    assert metrics['max_drawdown_3y_pct'] > metrics['max_drawdown_1y_pct'] > 0
    assert metrics['turnover_20d_yi'] == 2
    assert metrics['risk_as_of'] == '2026-11-05'


def test_risk_short_stale_invalid_data_never_becomes_zero():
    from tradingagents.dataflows.dividend_screen import price_risk
    rows = risk_history()
    metrics, notes = price_risk(rows[-50:], date(2026, 11, 6))
    assert metrics['volatility_1y_pct'] is None and len(notes) == 2
    for bad in (rows[:-10], rows + rows[-1:]):
        metrics, notes = price_risk(bad, date(2026, 11, 6))
        assert all(v is None for v in metrics.values()) and notes
    rows[-1]['成交额'] = None
    metrics, notes = price_risk(rows, date(2026, 11, 6))
    assert metrics['turnover_20d_yi'] is None
    assert metrics['volatility_3y_pct'] is not None and notes


def test_payment_timing_does_not_inflate_annual_screen():
    data = inputs()
    template = data['dividends'][-1]
    data['dividends'] = data['dividends'][:-1] + [
        {**template, '报告期': '2025-06-30', '除权除息日': '2025-12-15',
         '最新公告日期': '2025-12-01', '现金分红-现金分红比例': 1.414},
        {**template, '除权除息日': '2026-05-13', '现金分红-现金分红比例': 1.689},
        {**template, '报告期': '2026-06-30', '除权除息日': '2026-10-21',
         '最新公告日期': '2026-10-01', '现金分红-现金分红比例': 1.511},
    ]
    row = result(data, price=8.44)
    assert row['metrics']['dividend_annual'] == pytest.approx(.3103)
    assert row['metrics']['yield_pct'] == pytest.approx(3.676540284)
    assert row['metrics']['yield_ttm_pct'] == pytest.approx(5.46682464)
    assert row['status'] == 'fail'


def test_interim_only_is_not_a_complete_annual_dividend():
    data = inputs()
    data['dividends'][-1]['报告期'] = '2025-06-30'
    row = result(data)
    assert row['metrics']['yield_pct'] is None
    assert row['metrics']['yield_ttm_pct'] == 5
    assert row['status'] == 'pending'


def test_legacy_snapshot_recalculated_without_overwriting(tmp_path):
    row = result(price=12.5)
    row['metrics'].pop('yield_basis')
    row['metrics']['yield_pct'] = 8
    row['checks'] = [c for c in row['checks'] if '最近完整年度' not in c['label']]
    row['checks'].append({'label': '税前TTM股息率>4%', 'status': 'pass',
                          'required': True, 'detail': ''})
    path = tmp_path / 'dividend-screen.json'
    atomic_json(path, snapshot([row]))
    original = path.read_bytes()
    api = DesktopAPI(DesktopStore(tmp_path))
    projected = api.dividend_screen()['data']['items'][0]
    assert projected['metrics']['yield_pct'] == 4
    assert projected['status'] == 'fail'
    assert path.read_bytes() == original
    assert not any(c['label'] == '税前TTM股息率>4%' for c in projected['checks'])


@pytest.mark.parametrize('events', [None, 'invalid', [{}], []])
def test_legacy_snapshot_missing_events_cannot_keep_ttm_yield(tmp_path, events):
    row = result()
    row['metrics'].pop('yield_basis')
    row['dividend_events'] = events
    atomic_json(tmp_path / 'dividend-screen.json', snapshot([row]))
    projected = DesktopAPI(DesktopStore(tmp_path)).dividend_screen()['data']['items'][0]
    assert projected['metrics']['yield_pct'] is None
    assert projected['status'] == 'pending'


def test_zero_cash_final_event_survives_snapshot_roundtrip():
    from tradingagents.dataflows.dividend_screen import annual_snapshot

    data = inputs()
    event = data['dividends'][-1]
    data['dividends'][-1] = {**event, '现金分红-现金分红比例': 0}
    data['dividends'].append({**event, '报告期': '2025-06-30',
                              '除权除息日': '2025-12-15', '最新公告日期': '2025-12-01'})
    row = result(data)
    assert row['metrics']['yield_pct'] == 5
    row['metrics'].pop('yield_basis')
    assert annual_snapshot(snapshot([row]))['items'][0]['metrics']['yield_pct'] == 5


def test_cash_share_basis_handles_later_split_duplicate_and_future_proposal():
    data = inputs()
    last = data['dividends'][-1]
    last['送转股份-送转总比例'] = 2
    data['dividends'].extend([dict(last), {**last, '报告期': '2026-06-30',
        '除权除息日': '2026-09-01', '最新公告日期': '2026-08-01',
        '现金分红-现金分红比例': None, '送转股份-送转总比例': 5,
        '现金分红-现金分红比例描述': '10转5.00股'}])
    row = result(data)
    assert row['metrics']['yield_pct'] == pytest.approx(5 / 1.2 / 1.5)
    assert row['metrics']['dividend_ttm'] == pytest.approx(.5 / 1.2 / 1.5)
    data['dividends'].append({**last, '报告期': '2026-12-31',
        '除权除息日': '2027-06-01', '最新公告日期': '2027-05-01',
        '送转股份-送转总比例': 10})
    assert result(data)['metrics']['yield_pct'] == pytest.approx(5 / 1.2 / 1.5)
    data['dividends'].append({**last, '现金分红-现金分红比例': 6})
    assert result(data)['metrics']['yield_pct'] is None


@pytest.mark.parametrize('description', ['10转3.00', '10送3.00股'])
def test_explicit_pure_share_plan_resolves_missing_cash(description):
    data = inputs()
    data['dividends'][2].update({'现金分红-现金分红比例': None,
        '送转股份-送转总比例': 3, '现金分红-现金分红比例描述': description})
    row = result(data)
    assert row['metrics']['yield_pct'] == 5  # earlier split must not dilute later cash
    assert any(e.get('cash_parse_basis') for e in row['dividend_events'])
    data['dividends'][2]['送转股份-送转总比例'] = 4
    assert result(data)['metrics']['yield_pct'] is None


@pytest.mark.parametrize(('failure', 'label'), [
    ('empty', '返回空表'), ('timeout', '请求超时'),
    ('exception', '接口进程异常'), ('invalid', '返回格式无效')])
def test_source_error_categories_do_not_expose_process_output(tmp_path, monkeypatch, failure, label):
    import subprocess
    from types import SimpleNamespace

    from tradingagents.dataflows.errors import VendorUnavailableError

    def run(*args, **kwargs):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('secret', 50, stderr='secret')
        if failure == 'exception':
            raise subprocess.CalledProcessError(1, 'secret', stderr='secret')
        return SimpleNamespace(stdout='[]' if failure == 'empty' else '{}')

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr('time.sleep', lambda _: None)
    with pytest.raises(VendorUnavailableError) as exc:
        Source(tmp_path).fetch('stock_fhps_detail_em', '600941')
    assert label in str(exc.value)
    assert 'secret' not in str(exc.value)


def test_staged_collection_keeps_unknown_and_stops_only_confirmed_low_yield(tmp_path, monkeypatch, capsys):
    from datetime import datetime

    import tradingagents.dataflows.dividend_screen as screen

    calls = []
    today = date(2026, 11, 6)

    class Clock(date):
        @classmethod
        def today(cls):
            return today

    class FakeSource:
        def __init__(self, *args, **kwargs):
            pass

        def quotes(self, codes):
            return {c: {'price': 12.5, 'market_cap_yi': 500,
                        'quote_time': '20261106150000'} for c in codes}

        def fetch(self, endpoint, code):
            calls.append((endpoint, code))
            rows = inputs()['dividends'] if endpoint == screen.ENDPOINTS['dividends'] else []
            if code == '000002' and endpoint == screen.ENDPOINTS['dividends']:
                rows = []
            return {'endpoint': endpoint, 'symbol': code, 'rows': rows,
                    'fetched_at': datetime.now().isoformat()}

    monkeypatch.setattr(screen, 'date', Clock)
    monkeypatch.setattr(screen, 'Source', FakeSource)
    monkeypatch.setattr(screen, 'exchange_roster', lambda *args: {})
    monkeypatch.setattr(screen, 'discover_market', lambda *args, **kwargs: (
        [('000001', 'low', '通信'), ('000002', 'unknown', '通信')],
        {'scanned': 2, 'excluded_small_cap': 0}, {}))
    payload = screen.run(tmp_path)
    assert '000001 low: fail' in capsys.readouterr().out.splitlines()
    low, unknown = payload['items']
    assert low['code'] == '000001' and low['metrics']['yield_pct'] == 4
    assert low['collection_stage'] == 'yield_excluded'
    assert unknown['collection_stage'] == 'full'
    assert unknown['metrics']['yield_pct'] is None
    assert [endpoint for endpoint, code in calls if code == '000001'] == [screen.ENDPOINTS['dividends']]
    assert (screen.HISTORY, '000002') in calls
    assert payload['discovery']['yield_excluded'] == 1


def core_row():
    from tradingagents.dataflows.dividend_screen import core_profile

    data = inputs()
    for report in data['profit']:
        report['BASIC_EPS'] = 1
    row = result(data)
    row['metrics'].update(risk_as_of='2026-11-06', volatility_1y_pct=25,
                          volatility_3y_pct=25, max_drawdown_1y_pct=25,
                          max_drawdown_3y_pct=35, turnover_20d_yi=1)
    assert core_profile(row, date(2026, 11, 6))['status'] == 'pass'
    return row


@pytest.mark.parametrize(('metric', 'value', 'expected'), [
    ('payout_ratio_pct', 80, 'pass'), ('payout_ratio_pct', 80.01, 'fail'),
    ('payout_ratio_pct', None, 'pending'), ('payout_ratio_pct', 0, 'fail'),
    ('volatility_1y_pct', 25.01, 'fail'), ('volatility_3y_pct', None, 'pending'),
    ('max_drawdown_1y_pct', -1, 'fail'), ('max_drawdown_3y_pct', 35.01, 'fail'),
    ('turnover_20d_yi', .99, 'fail'), ('risk_as_of', '2026-11-01', 'pending'),
    ('risk_as_of', '2026-11-07', 'pending'),
])
def test_core_thresholds_and_unknown_evidence(metric, value, expected):
    from tradingagents.dataflows.dividend_screen import core_profile

    row = core_row()
    row['metrics'][metric] = value
    assert core_profile(row, date(2026, 11, 6))['status'] == expected


def test_core_evidence_reports_actual_risk_and_missing_values():
    from tradingagents.dataflows.dividend_screen import core_profile

    row = core_row()
    row['metrics'].update(volatility_1y_pct=31.234, max_drawdown_3y_pct=None)
    checks = {c['label']: c for c in core_profile(row, date(2026, 11, 6))['checks']}
    volatility = checks['1年年化波动率≤25%']
    assert volatility['status'] == 'fail'
    assert '实际 31.23%' in volatility['detail']
    assert '2026-11-06' in volatility['detail']
    missing = checks['3年最大回撤≤35%']
    assert missing['status'] == 'pending'
    assert '实际值缺失' in missing['detail']
    assert '0.00%' not in missing['detail']
    assert '实际 1.00亿元' in checks['20日日均成交额≥1亿元']['detail']
    assert '2025年' in checks['近3年每股分红每年降幅≤20%']['detail']


@pytest.mark.parametrize(('dps', 'expected'), [(.4, 'pass'), (.399, 'fail'), (None, 'pending')])
def test_core_dividend_trend(dps, expected):
    from tradingagents.dataflows.dividend_screen import core_profile

    row = core_row()
    row['metrics']['dividend_annual_history'][0]['dps'] = dps
    assert core_profile(row, date(2026, 11, 6))['status'] == expected


def test_cumulative_cashflow_allows_a_positive_volatile_year():
    data = inputs()
    for report, cfo in zip(data['cashflow'], (10e9, 10e9, 1e9), strict=True):
        report['NETCASH_OPERATE'] = cfo
    assert result(data)['status'] == 'pass'
    data['cashflow'][-1]['NETCASH_OPERATE'] = 0
    assert result(data)['status'] == 'fail'


def test_payout_requires_comparable_eps_and_share_basis():
    data = inputs()
    data['profit'][-1]['BASIC_EPS'] = 1
    assert result(data)['metrics']['payout_ratio_pct'] == 50
    data['dividends'][-1]['每股收益'] = 2
    assert result(data)['metrics']['payout_ratio_pct'] is None
    del data['dividends'][-1]['每股收益']
    data['dividends'][-1]['送转股份-送转总比例'] = 2
    assert result(data)['metrics']['payout_ratio_pct'] is None


def test_cached_projection_requires_original_packet_identity(tmp_path):
    import hashlib

    from tradingagents.dataflows.dividend_screen import ENDPOINTS, annual_snapshot, market_symbol

    data = inputs()
    data['profit'][-1]['BASIC_EPS'] = 1
    row = core_row()
    row['metrics'].pop('sustainability_basis')
    row['metrics'].pop('payout_ratio_pct')
    row['provenance'] = {kind: {'fetched_at': '2026-11-06T18:00:00'}
                         for kind in ('profit', 'cashflow', 'balance')}
    for kind in row['provenance']:
        endpoint, symbol = ENDPOINTS[kind], market_symbol(row['code'])
        key = hashlib.sha256(f'{endpoint}:{symbol}:'.encode()).hexdigest()[:20]
        atomic_json(tmp_path / f'{key}.json', {'endpoint': endpoint, 'symbol': symbol,
                    'fetched_at': '2026-11-06T18:00:00', 'rows': data[kind]})
    original = snapshot([row])
    projected = annual_snapshot(original, tmp_path)
    assert projected['items'][0]['metrics']['payout_ratio_pct'] == 50
    assert projected['items'][0]['core']['status'] == 'pass'
    assert 'payout_ratio_pct' not in original['items'][0]['metrics']
    assert annual_snapshot(projected, tmp_path) == projected
    original['items'][0]['provenance']['profit']['fetched_at'] = 'different'
    unmatched = annual_snapshot(original, tmp_path)
    assert unmatched['items'][0]['core']['status'] == 'pending'
    assert 'payout_ratio_pct' not in unmatched['items'][0]['metrics']
def test_trading_calendar_rejects_incomplete_and_out_of_range_data():
    from datetime import date, timedelta

    from tradingagents.dataflows.dividend_screen import calendar_target, trading_calendar

    rows = [{'trade_date': (date(2026, 1, 1) + timedelta(days=n)).isoformat()}
            for n in range(365) if (date(2026, 1, 1) + timedelta(days=n)).weekday() < 5]

    class Source:
        def fetch(self, endpoint, symbol):
            return {'rows': rows}

    calendar = trading_calendar(Source())
    assert calendar_target(calendar, date(2026, 11, 8)) == date(2026, 11, 6)
    assert calendar_target(calendar, date(2027, 1, 1)) is None
    assert calendar_target(None, date(2026, 11, 8)) is None
    calendar['dates'].pop(0)
    assert calendar_target(calendar, date(2026, 11, 8)) is None
    rows[:] = rows[:10]
    assert trading_calendar(Source())['status'] == 'unavailable'


def test_trading_calendar_keeps_recent_coverage_and_tolerates_provider_failure():
    from datetime import timedelta

    from tradingagents.dataflows.dividend_screen import trading_calendar
    from tradingagents.dataflows.errors import VendorUnavailableError

    rows = [{'trade_date': (date(2022, 1, 1) + timedelta(days=n)).isoformat()}
            for n in range(1800) if (date(2022, 1, 1) + timedelta(days=n)).weekday() < 5]
    rows.insert(0, {'trade_date': '1992-05-09'})  # Historical Saturday trading.

    class Source:
        def fetch(self, endpoint, symbol):
            return {'rows': rows}

    calendar = trading_calendar(Source())
    assert calendar['status'] == 'available'
    assert len(calendar['dates']) == 800
    assert calendar['first_date'] > '2022-01-01'
    rows.append({'trade_date': '2026-12-05'})
    assert trading_calendar(Source())['status'] == 'unavailable'

    class Unavailable:
        def fetch(self, endpoint, symbol):
            raise VendorUnavailableError('offline')

    assert trading_calendar(Unavailable())['status'] == 'unavailable'
