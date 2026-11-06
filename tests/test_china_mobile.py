"""Small source fixtures; no live downloads."""
from datetime import date
from unittest.mock import Mock

import pytest

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import china_mobile as mobile


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(mobile, '_today', lambda: date(2026, 11, 5))


@pytest.mark.parametrize('ticker', ['0941.HK', '600519.SH', '600941', 'AAPL'])
def test_other_issuers_and_markets_rejected_without_requests(monkeypatch, ticker):
    session = Mock()
    monkeypatch.setattr(mobile.requests, 'Session', session)
    with pytest.raises(NoMarketDataError):
        mobile.get_company_operating_history(ticker)
    session.assert_not_called()


def test_historical_and_future_requests_withheld_without_requests(monkeypatch):
    session = Mock()
    monkeypatch.setattr(mobile.requests, 'Session', session)
    for day in ['2026-11-04', '2026-11-06']:
        result = mobile.get_company_operating_history('600941.SH', day)
        assert 'DATA_UNAVAILABLE' in result and 'withheld' in result
    session.assert_not_called()


def report(year):
    return [f'CHINA MOBILE LIMITED INTERIM REPORT {year}',
            'Mobile ARPU (average revenue per user per month) was RMB45.1. '
            'Household customer blended ARPU reached RMB46.8.',
            f'In the first half of {year}, our capital expenditure was RMB61.0 billion.']


def test_official_narrative_metrics_physical_pages_and_wrong_year():
    text = '\n'.join(mobile._interim_metrics(2026, report(2026)))
    assert '2026H1' in text and '61.0 RMB billion' in text
    assert '45.1' in text and '46.8' in text and '第3页' in text
    assert not mobile._interim_metrics(2025, report(2026))
    assert not mobile._interim_metrics(2026, ['Mobile ARPU was RMB45.1'])


def test_capital_commitments_and_plans_are_not_actuals():
    pages = ['CHINA MOBILE LIMITED INTERIM REPORT 2026',
             'Capital commitments RMB29 billion. We expect capital expenditure RMB100 billion. '
             'Capital expenditure cash payments RMB70 billion.']
    assert not mobile._interim_metrics(2026, pages)


def customer(year, quarter, old=False, unit='万户'):
    layout = ('移动业务\n客户总数 100,887.4\n5G 网络客户数 62,235.3\n有线宽带业务\n客户总数 32,876.6'
              if old else '移动客户数 101,405.5\n5G 网络客户数 70,228.2\n宽带联网 1 客户数 34,226.5')
    return [f'中国移动有限公司 股票代码：600941\n{year}年第{quarter}季度客户数据公告\n单位：{unit}\n' + layout]


def test_customers_keep_broadband_definitions_and_validate_period_and_units():
    old = '\n'.join(mobile._customer_metrics('2025年第三季度客户数据公告', customer(2025, '三', True)))
    new = '\n'.join(mobile._customer_metrics('2026年第三季度客户数据公告', customer(2026, '三')))
    assert '有线宽带客户数' in old and '宽带联网客户数' not in old
    assert '宽带联网客户数' in new and '有线宽带客户数' not in new
    assert '5G网络客户数' in old and '62,235.3 万户' in old
    assert not mobile._customer_metrics('2026年第三季度客户数据公告', customer(2025, '三'))
    assert not mobile._customer_metrics('2026年第三季度客户数据公告', customer(2026, '三', unit='户'))
    assert not mobile._customer_metrics('2026年第三季度客户数据公告', ['600941 单位：万户 移动客户数 123'])


def test_quarter_selection_deduplicates_and_excludes_future_and_unknown():
    rows = [{'title': title, 'day': day, 'url': title} for title, day in [
        ('2026年第三季度客户数据公告', date(2026,10,21)),
        ('2026年第三季度客户数据公告（更正）', date(2026,10,22)),
        ('2026年第四季度客户数据公告', date(2026,10,23)),
        ('2026年第二季度客户数据公告摘要', date(2026,8,14)),
        ('客户数据公告', date(2026,8,14)),
        ('2026年第二季度客户数据公告', date(2026,11,6)),
        ('2025年第四季度客户数据公告', date(2026,3,27)),
    ]]
    selected = mobile._select_customers(rows, date(2026,11,5))
    assert [r['title'] for r in selected] == ['2026年第三季度客户数据公告（更正）', '2025年第四季度客户数据公告']


def test_partial_failure_still_returns_metrics_and_provenance(monkeypatch):
    monkeypatch.setattr(mobile, '_official_pdf_pages', lambda client, url: report(2026) if '2026' in url else [])
    query = Mock(return_value=([{'title': '2026年第三季度客户数据公告', 'day': date(2026,10,21), 'url': 'https://static.cninfo.com.cn/a.pdf'}], False))
    monkeypatch.setattr(mobile.cninfo, '_announcements', query)
    monkeypatch.setattr(mobile.cninfo, '_pdf_pages', lambda *a: customer(2026, '三'))
    result = mobile.get_company_operating_history('600941.SS')
    assert '61.0 RMB billion' in result and '70,228.2 万户' in result
    assert 'publication_date=unknown' in result and '2026-10-21' in result and 'fetched_at=' in result
    assert 'missing' in result and 'VAT' in result and '不计算' in result
    assert len(result) <= 20000
    assert query.call_args.kwargs['keyword'] == '客户'
    assert query.call_args.args[2] == date(2026,11,5) - mobile.timedelta(days=1095)


def test_empty_and_failed_sources_are_explicitly_unavailable(monkeypatch):
    monkeypatch.setattr(mobile, '_official_pdf_pages', lambda *a: [])
    monkeypatch.setattr(mobile.cninfo, '_announcements', Mock(side_effect=VendorUnavailableError('offline')))
    result = mobile.get_company_operating_history('600941.SH')
    assert result.startswith('DATA_UNAVAILABLE') and 'missing' in result


def test_pdf_security_stream_bounds_and_ttl(monkeypatch, tmp_path):
    monkeypatch.setattr(mobile, 'get_config', lambda: {'data_cache_dir': str(tmp_path)})
    client = Mock()
    for url in ['http://www.chinamobileltd.com/a.pdf', 'https://evil.com/a.pdf',
                'https://www.chinamobileltd.com@evil.com/a.pdf']:
        with pytest.raises(VendorUnavailableError):
            mobile._official_pdf_pages(client, url)
    client.get.assert_not_called()
    response = Mock(status_code=302, headers={})
    client.get.return_value = response
    with pytest.raises(VendorUnavailableError):
        mobile._official_pdf_pages(client, 'https://www.chinamobileltd.com/en/ir/reports/ir2026.pdf')
    assert client.get.call_args.kwargs['allow_redirects'] is False
    response.close.assert_called_once()


def test_arpu_narrative_may_contain_decimal_growth_before_level():
    pages = report(2025)
    pages[1] = 'Household customer blended ARPU increased by 2.3% year-on-\nyear to RMB44.4.'
    assert '44.4 RMB/user/month' in '\n'.join(mobile._interim_metrics(2025, pages))


def test_pdf_cache_expires_and_rejects_oversized_response(monkeypatch, tmp_path):
    import hashlib
    import json
    url = 'https://www.chinamobileltd.com/en/ir/reports/ir2026.pdf'
    monkeypatch.setattr(mobile, 'get_config', lambda: {'data_cache_dir': str(tmp_path)})
    monkeypatch.setattr(mobile.time, 'time', lambda: 100000)
    path = tmp_path / 'china-mobile' / 'text-v2' / (hashlib.sha256(url.encode()).hexdigest() + '.json')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'fetched_epoch': 99999, 'pages': ['cached']}))
    client = Mock()
    assert mobile._official_pdf_pages(client, url) == ['cached']
    client.get.assert_not_called()
    monkeypatch.setattr(mobile.time, 'time', lambda: 100000 + mobile.CACHE_TTL_SECONDS)
    response = Mock(status_code=200, headers={'Content-Length': str(mobile.MAX_PDF_BYTES + 1)})
    client.get.return_value = response
    with pytest.raises(VendorUnavailableError, match='byte limit'):
        mobile._official_pdf_pages(client, url)
    response.close.assert_called_once()


def test_keyword_is_passed_to_cninfo_query_without_changing_default():
    from tradingagents.dataflows.vendors import cninfo
    response = Mock()
    response.json.return_value = {'stockList': [{'code': '600941', 'orgId': 'org'}]}
    post = Mock()
    post.json.return_value = {'announcements': [], 'hasMore': False}
    client = Mock()
    client.get.return_value, client.post.return_value = response, post
    for kwargs, expected in [({}, ''), ({'keyword': '客户'}, '客户')]:
        cninfo._announcements(client, '600941', date(2026, 1, 1), date(2026, 11, 5), 'sse', **kwargs)
        assert client.post.call_args.kwargs['data']['searchkey'] == expected


def test_five_latest_reporting_periods_win_over_late_old_corrections():
    rows = []
    for year, q in [(2026,3), (2026,2), (2026,1), (2025,4), (2025,3), (2024,1)]:
        rows.append({'title': f'{year}年第{q}季度客户数据公告', 'day': date(2026,10,20 if year > 2024 else 30), 'url': str((year,q))})
    selected = mobile._select_customers(rows, date(2026,11,5))
    assert [mobile._quarter(r['title']) for r in selected] == [(2026,3), (2026,2), (2026,1), (2025,4), (2025,3)]


def test_pdf_stream_limit_is_enforced_without_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(mobile, 'get_config', lambda: {'data_cache_dir': str(tmp_path)})
    monkeypatch.setattr(mobile, 'MAX_PDF_BYTES', 8)
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = [b'%PDF-', b'too many bytes']
    client = Mock()
    client.get.return_value = response
    with pytest.raises(VendorUnavailableError, match='download bounds'):
        mobile._official_pdf_pages(client, 'https://www.chinamobileltd.com/en/ir/reports/ir2026.pdf')
    response.close.assert_called_once()


def test_missing_pdf_dependency_is_classified_without_request(monkeypatch):
    import sys

    from tradingagents.dataflows.errors import VendorNotConfiguredError
    monkeypatch.setitem(sys.modules, 'pypdf', None)
    client = Mock()
    with pytest.raises(VendorNotConfiguredError, match='requires pypdf'):
        mobile._official_pdf_pages(client, 'https://www.chinamobileltd.com/en/ir/reports/ir2026.pdf')
    client.get.assert_not_called()


@pytest.mark.parametrize('error', [KeyError('stockList'), TypeError('unexpected index shape')])
def test_quarter_index_schema_failure_keeps_official_h1_metrics(monkeypatch, error):
    monkeypatch.setattr(mobile, '_official_pdf_pages', lambda client, url: report(2026) if '2026' in url else [])
    monkeypatch.setattr(mobile.cninfo, '_announcements', Mock(side_effect=error))
    result = mobile.get_company_operating_history('600941.SS')
    assert not result.startswith('DATA_UNAVAILABLE:')
    assert '2026H1 实际资本开支：61.0 RMB billion' in result
    assert '移动ARPU：45.1' in result
    assert 'missing: CNInfo季度客户查询不可用' in result


@pytest.mark.parametrize('bound', ['pages', 'text', 'time'])
def test_pdf_truncation_is_explicit_and_survives_cache(monkeypatch, tmp_path, bound):
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(mobile, 'get_config', lambda: {'data_cache_dir': str(tmp_path)})
    monkeypatch.setattr(mobile, 'MAX_PDF_PAGES', 2)
    monkeypatch.setattr(mobile, 'MAX_TEXT_CHARS', 20)
    texts = ['a', 'b', 'c'] if bound == 'pages' else ['a' * 21]
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=lambda _: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda text=text: text) for text in texts])))
    if bound == 'time':
        ticks = iter([0, 0, 91])
        monkeypatch.setattr(mobile.time, 'monotonic', lambda: next(ticks))
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = [b'%PDF fake']
    client = Mock()
    client.get.return_value = response
    url = 'https://www.chinamobileltd.com/en/ir/reports/ir2026.pdf'
    pages = mobile._official_pdf_pages(client, url)
    assert 'PDF提取截断' in pages[-1]
    assert mobile._official_pdf_pages(client, url) == pages
    client.get.assert_called_once()


def test_operating_history_displays_truncated_source_warning(monkeypatch):
    monkeypatch.setattr(mobile, '_official_pdf_pages', lambda *a:
                        report(2026) + ['[PDF提取截断：达到页数、文字或时间上限]'])
    monkeypatch.setattr(mobile.cninfo, '_announcements', lambda *a, **kw: ([], False))
    result = mobile.get_company_operating_history('600941.SS')
    assert 'PDF提取截断' in result
    assert '61.0 RMB billion' in result


def test_legacy_cache_without_truncation_notice_is_replaced_in_v2(monkeypatch, tmp_path):
    import hashlib
    import json
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(mobile, 'get_config', lambda: {'data_cache_dir': str(tmp_path)})
    monkeypatch.setattr(mobile.time, 'time', lambda: 100000)
    monkeypatch.setattr(mobile, 'MAX_PDF_PAGES', 2)
    monkeypatch.setattr(mobile, 'MAX_TEXT_CHARS', 20)
    url = 'https://www.chinamobileltd.com/en/ir/reports/ir2026.pdf'
    path = tmp_path / 'china-mobile' / 'text-v1' / (hashlib.sha256(url.encode()).hexdigest() + '.json')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'fetched_epoch': 99999, 'pages': ['silently clipped legacy text']}))
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=lambda _: SimpleNamespace(
        pages=[SimpleNamespace(extract_text=lambda: 'fresh')])))
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = [b'%PDF fake']
    client = Mock()
    client.get.return_value = response
    assert mobile._official_pdf_pages(client, url) == ['fresh']
    assert path.exists()
    refreshed = path.parent.parent / 'text-v2' / path.name
    assert json.loads(refreshed.read_text())['pages'] == ['fresh']
    assert mobile._official_pdf_pages(client, url) == ['fresh']
    client.get.assert_called_once()
