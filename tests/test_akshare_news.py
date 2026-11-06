"""Resolved company names lead A-share news search; listing codes remain a fallback."""
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.dataflows import date_window, router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors import akshare
from tradingagents.dataflows.vendors.akshare import common

TODAY = '2026-11-05'


@pytest.fixture(autouse=True)
def current_date(monkeypatch):
    monkeypatch.setattr(date_window, 'get_current_date', lambda: TODAY)


def article(title, date='2026-11-04 12:00:00', url='https://example.com/1'):
    return {'新闻标题': title, '发布时间': date, '新闻内容': '公开报道内容',
            '文章来源': 'Example', '新闻链接': url}


def responses(monkeypatch, code_rows, name_rows, name='中国移动'):
    queries = []

    def request(endpoint, **params):
        if endpoint == 'stock_individual_info_em':
            return pd.DataFrame({'item': ['股票简称'], 'value': [name]})
        assert endpoint == 'stock_news_em'
        queries.append(params['symbol'])
        return pd.DataFrame(code_rows if params['symbol'] == '600941' else name_rows)

    monkeypatch.setattr(common, 'request', request)
    return queries


def test_resolved_name_is_preferred_even_when_code_has_usable_news(monkeypatch):
    queries = responses(monkeypatch, [article('600941发布经营数据')], [article('中国移动签署战略合作协议')])
    out = akshare.get_news('600941.SS', '2026-10-29', '2026-11-05')
    assert '中国移动签署战略合作协议' in out and 'company name first' in out
    assert '600941发布经营数据' not in out
    assert queries == ['中国移动']


@pytest.mark.parametrize('name_rows', [[], [article('行业资金流')],
                                      [article('中国移动旧消息', '2026-10-01 12:00:00')],
                                      [article('中国移动未来消息', '2026-11-06 00:00:00')]])
def test_code_search_runs_when_name_has_no_usable_window_news(monkeypatch, name_rows):
    queries = responses(monkeypatch, [article('600941发布经营数据')], name_rows)
    out = akshare.get_news('600941.SS', '2026-10-29', '2026-11-05')
    assert '600941发布经营数据' in out and 'listing-code fallback' in out
    assert queries == ['中国移动', '600941']


def test_name_results_still_require_dates_company_match_and_deduplication(monkeypatch):
    responses(monkeypatch, [], [
        article('中国移动合作', url='https://example.com/a'),
        article('中国移动合作', url='https://example.com/reprint'),
        article('中国移动合作转载', url='https://example.com/a'),
        article('中国移动未来消息', date='2026-11-06 00:00:00', url='https://example.com/future'),
        article('其他公司消息', url='https://example.com/other'),
    ])
    out = akshare.get_news('600941.SS', '2026-10-29', '2026-11-05')
    assert out.count('### ') == 1
    assert '未来消息' not in out and '其他公司消息' not in out
    assert 'parent group' in out and 'independent events' in out


def test_unknown_name_does_not_trigger_a_guessed_query(monkeypatch):
    queries = responses(monkeypatch, [], [], name=None)
    with pytest.raises(VendorUnavailableError, match='not an absence'):
        akshare.get_news('600941.SS', '2026-10-29', '2026-11-05')
    assert queries == ['600941']


def test_both_queries_unusable_continue_the_configured_vendor_chain(monkeypatch):
    queries = responses(monkeypatch, [article('行业资金流')], [article('中国移动旧消息', '2026-10-01')])
    fallback = Mock(return_value='usable Yahoo news')
    monkeypatch.setitem(router.VENDOR_METHODS['get_news'], 'yfinance', fallback)
    with run_config({'data_vendors': {'news_data': 'akshare,yfinance'}}):
        out = router.route_to_vendor('get_news', '600941.SS', '2026-10-29', '2026-11-05')
    assert out == 'usable Yahoo news'
    assert queries == ['中国移动', '600941']
    fallback.assert_called_once()


def test_name_lookup_failure_still_uses_listing_code(monkeypatch):
    queries = []

    def request(endpoint, **params):
        if endpoint == 'stock_individual_info_em':
            raise VendorUnavailableError('profile lookup failed')
        queries.append(params['symbol'])
        return pd.DataFrame([article('600941发布经营数据')])

    monkeypatch.setattr(common, 'request', request)
    out = akshare.get_news('600941.SS', '2026-10-29', '2026-11-05')
    assert '600941发布经营数据' in out
    assert queries == ['600941']


def test_name_query_failure_still_tries_listing_code(monkeypatch):
    queries = []

    def request(endpoint, **params):
        if endpoint == 'stock_individual_info_em':
            return pd.DataFrame({'item': ['股票简称'], 'value': ['中国移动']})
        queries.append(params['symbol'])
        if params['symbol'] == '中国移动':
            raise VendorUnavailableError('name search temporarily failed')
        return pd.DataFrame([article('600941发布经营数据')])

    monkeypatch.setattr(common, 'request', request)
    out = akshare.get_news('600941.SS', '2026-10-29', '2026-11-05')
    assert '600941发布经营数据' in out
    assert queries == ['中国移动', '600941']


def news_row(title, when, content='中国移动公告内容'):
    return {'新闻标题': title, '新闻内容': content, '发布时间': when,
            '文章来源': 'Test', '新闻链接': f'https://example.com/{title}'}


def news_fetch(monkeypatch, rows):
    def request(endpoint, **kwargs):
        if endpoint == 'stock_individual_info_em':
            return pd.DataFrame({'item': ['股票简称'], 'value': ['中国移动']})
        assert endpoint == 'stock_news_em' and kwargs['symbol'] in ('600941', '中国移动')
        return pd.DataFrame(rows)
    monkeypatch.setattr(common, 'request', request)


def test_news_requires_company_headline_and_shanghai_date_window(monkeypatch):
    news_fetch(monkeypatch, [
        news_row('中国移动有效新闻', '2026-11-05 00:01:00'),
        news_row('600941有效代码', '2026-11-05 23:59:59'),
        news_row('中国移动次日新闻', '2026-11-06 00:00:00'),
        news_row('中国移动旧新闻', '2026-10-28 23:59:59'),
        news_row('行业资金流', '2026-11-04 10:00:00'),
        news_row('中国移动无日期', None),
    ])
    out = akshare.get_news('600941.SS', '2026-10-29', TODAY)
    assert '有效新闻' in out and '有效代码' in out
    assert '次日新闻' not in out and '旧新闻' not in out and '行业资金流' not in out and '无日期' not in out
    assert 'sample' in out and 'Asia/Shanghai' in out


def test_missing_relevant_news_is_not_claimed_as_no_events(monkeypatch):
    news_fetch(monkeypatch, [news_row('美国大盘', '2026-11-04 10:00:00')])
    with pytest.raises(VendorUnavailableError, match='not an absence'):
        akshare.get_news('600941.SS', '2026-10-29', TODAY)


@pytest.mark.parametrize('rows', [[], [news_row('行业资金流', '2026-11-04 10:00:00')]])
def test_news_coverage_gap_tries_the_next_configured_vendor(monkeypatch, rows):
    news_fetch(monkeypatch, rows)
    fallback = Mock(return_value='usable Yahoo company news')
    monkeypatch.setitem(router.VENDOR_METHODS['get_news'], 'yfinance', fallback)
    with run_config({'data_vendors': {'news_data': 'akshare,yfinance'}}):
        out = router.route_to_vendor('get_news', '600941.SS', '2026-10-29', TODAY)
    assert out == 'usable Yahoo company news'
    fallback.assert_called_once_with('600941.SS', '2026-10-29', TODAY)


def test_news_coverage_gap_without_a_usable_fallback_remains_unavailable(monkeypatch):
    news_fetch(monkeypatch, [])
    fallback = Mock(side_effect=NoMarketDataError('600941.SS'))
    monkeypatch.setitem(router.VENDOR_METHODS['get_news'], 'yfinance', fallback)
    with run_config({'data_vendors': {'news_data': 'akshare,yfinance'}}):
        out = router.route_to_vendor('get_news', '600941.SS', '2026-10-29', TODAY)
    assert out.startswith('DATA_UNAVAILABLE:') and 'not an absence' in out
    fallback.assert_called_once()


def test_news_coverage_gap_does_not_call_an_unselected_vendor(monkeypatch):
    news_fetch(monkeypatch, [])
    fallback = Mock(side_effect=AssertionError('unselected vendor'))
    monkeypatch.setitem(router.VENDOR_METHODS['get_news'], 'yfinance', fallback)
    with run_config({'tool_vendors': {'get_news': 'akshare'}}):
        out = router.route_to_vendor('get_news', '600941.SS', '2026-10-29', TODAY)
    assert out.startswith('DATA_UNAVAILABLE:') and 'not an absence' in out
    fallback.assert_not_called()
