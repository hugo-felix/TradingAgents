"""Bounded, point-in-time-safe public A-share attention and discussion sources."""
import json
import time
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
import requests

from tradingagents.dataflows.vendors import eastmoney_sentiment as vendor

TODAY = '2026-11-05'
NOW = datetime(2026, 11, 5, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(vendor, '_now', lambda: NOW)


def response(value, *, raw=False):
    result = Mock()
    body = value.encode() if raw else json.dumps(value).encode()
    result.headers = {}
    result.status_code = 200
    result.iter_content.return_value = [body]
    return result


def script(name, value):
    return '<script>var ' + name + '=' + json.dumps(value) + ';untrusted();</script>'


def row(pid=1, **changes):
    return dict(post_id=pid, stockbar_code='600941', post_type=0,
                post_publish_time='2026-11-05 10:00:00', post_title='公司讨论',
                post_click_count=10, post_comment_count=2, **changes)


def listing(rows, **changes):
    return script('article_list', dict(bar_code='600941', bar_quot_code='SH600941', re=rows, **changes))


def detail(item, **changes):
    return script('post_article', {**item, 'post_guba': {'stockbar_code': '600941'},
                                   'post_content': '<p>正文 <b>用户观点</b></p>', **changes})


def fetch(monkeypatch, values):
    call = Mock(side_effect=[v if isinstance(v, Exception) else response(v, raw=isinstance(v, str)) for v in values])
    monkeypatch.setattr(vendor.requests, 'request', call)
    return call


@pytest.mark.parametrize('fn', ['fetch_a_share_attention', 'fetch_a_share_discussions'])
@pytest.mark.parametrize('ticker,start,end', [
    ('AAPL', '2026-11-01', TODAY), ('0941.HK', '2026-11-01', TODAY),
    ('600941', '2026-11-01', TODAY), ('600941.SZ', '2026-11-01', TODAY),
    ('600941.SS', '2026-11-01', '2026-11-04'),
    ('600941.SS', 'bad', TODAY), ('600941.SS', TODAY, '2026-11-01'),
    ('600941.SS', TODAY, '2026-11-06'),
])
def test_invalid_or_historical_windows_never_request(monkeypatch, fn, ticker, start, end):
    call = fetch(monkeypatch, [])
    out = getattr(vendor, fn)(ticker, start, end)
    assert out.startswith('DATA_UNAVAILABLE')
    if end == '2026-11-04':
        assert 'withheld' in out
    call.assert_not_called()


def test_attention_keeps_valid_history_when_latest_identity_is_wrong(monkeypatch):
    call = fetch(monkeypatch, [
        {'data': {'srcSecurityCode': 'SZ000001', 'calcTime': '2026-11-05 10:00', 'rank': 2}},
        {'data': [{'calcTime': '2026-11-04', 'rank': 100},
                  {'calcTime': '2026-11-06', 'rank': 999},
                  {'calcTime': '2026-10-01', 'rank': 998}]},
    ])
    out = vendor.fetch_a_share_attention('600941.SH', '2026-10-01', TODAY)
    assert not out.startswith('DATA_UNAVAILABLE')
    assert '100' in out and '999' not in out and '998' not in out
    assert 'latest: missing' in out and 'getHisList' in out and 'fetched_at' in out
    assert call.call_args_list[0].kwargs['json']['srcSecurityCode'] == 'SH600941'


def test_attention_latest_and_empty_or_malformed_history(monkeypatch):
    fetch(monkeypatch, [{'data': {'srcSecurityCode': 'SH600941', 'calcTime': '2026-11-05 10:00:00', 'rank': 123, 'marketAllCount': 5400}}, {'data': []}])
    out = vendor.fetch_a_share_attention('600941.SS', TODAY, TODAY)
    assert '123' in out and '5400' in out and 'history: missing' in out
    assert 'not a sentiment score' in out


def test_daily_ranking_period_is_not_presented_as_a_midnight_snapshot(monkeypatch):
    fetch(monkeypatch, [{'data': None}, {'data': [{'calcTime': '2026-11-05', 'rank': 123}]}])
    out = vendor.fetch_a_share_attention('600941.SS', TODAY, TODAY)
    assert '2026-11-05: rank=123' in out
    assert '2026-11-05T00:00:00' not in out


@pytest.mark.parametrize('data', [None, [], {'rank': 1}, {'data': []}])
def test_no_valid_attention_is_unavailable(monkeypatch, data):
    fetch(monkeypatch, [data, data])
    assert vendor.fetch_a_share_attention('600941.SS', TODAY, TODAY).startswith('DATA_UNAVAILABLE')


def test_discussions_paginate_deduplicate_exclude_promoted_and_validate_bodies(monkeypatch):
    a, b = row(1), row(2)
    promoted = {**row(3), 'post_type': 1}
    other = {**row(4), 'stockbar_code': '000001'}
    future = {**row(5), 'post_publish_time': '2026-11-05 13:00:00'}
    old = {**row(6), 'post_publish_time': '2026-11-03 23:59:59', 'post_last_time': '2026-11-05 10:00:00'}
    call = fetch(monkeypatch, [listing([a, promoted, other, future, old]), listing([a, b]), detail(a), detail(b)])
    out = vendor.fetch_a_share_discussions('600941.SS', '2026-11-04', TODAY)
    assert not out.startswith('DATA_UNAVAILABLE')
    assert out.count('正文 用户观点') == 2
    assert 'news,600941,3.html' not in out and 'news,600941,6.html' not in out
    assert 'list,600941_2.html' in out and 'non-random' in out
    assert 'untrusted' in out and 'current snapshot' in out and '2026-11-05T10:00:00+08:00' in out
    assert call.call_count == 4


def test_discussions_list_titles_are_not_usable_bodies(monkeypatch):
    a = row()
    fetch(monkeypatch, [listing([a]), listing([]), detail(a, post_content='')])
    out = vendor.fetch_a_share_discussions('600941.SS', TODAY, TODAY)
    assert out.startswith('DATA_UNAVAILABLE') and '公司讨论' in out


@pytest.mark.parametrize('change', [
    {'post_id': 999}, {'post_guba': {'stockbar_code': '000001'}},
    {'post_publish_time': '2026-11-04 10:00:00'},
])
def test_detail_identity_or_publish_mismatch_is_unavailable(monkeypatch, change):
    a = row()
    fetch(monkeypatch, [listing([a]), listing([]), detail(a, **change)])
    assert vendor.fetch_a_share_discussions('600941.SS', TODAY, TODAY).startswith('DATA_UNAVAILABLE')


def test_partial_failure_keeps_body_and_reports_coverage(monkeypatch):
    a, b = row(1), row(2)
    fetch(monkeypatch, [listing([a, b]), requests.Timeout(), detail(a), '<html>changed</html>'])
    out = vendor.fetch_a_share_discussions('600941.SS', TODAY, TODAY)
    assert not out.startswith('DATA_UNAVAILABLE') and '正文 用户观点' in out
    assert 'coverage' in out and 'missing' in out


def test_wrong_forum_and_network_failures_gracefully_degrade(monkeypatch):
    fetch(monkeypatch, [script('article_list', {'bar_code': '000001', 'bar_quot_code': 'SZ000001', 're': [row()]}), requests.ConnectionError()])
    assert vendor.fetch_a_share_discussions('600941.SS', TODAY, TODAY).startswith('DATA_UNAVAILABLE')


def test_resource_budgets_and_finite_timeouts(monkeypatch):
    items = [row(i) for i in range(1, 21)]
    call = fetch(monkeypatch, [listing(items), listing(items), *[detail(i, post_content='x' * 20000) for i in items[:5]]])
    out = vendor.fetch_a_share_discussions('600941.SS', TODAY, TODAY)
    assert call.call_count == 7 and len(out) <= 16000
    for item in call.call_args_list:
        assert item.kwargs['timeout'] == (5, 10)
        assert item.kwargs['stream'] is True
    assert all(item.args[1].startswith('https://guba.eastmoney.com/') for item in call.call_args_list)


def test_oversized_response_is_rejected_and_closed(monkeypatch):
    huge = response('x' * (vendor.MAX_RESPONSE_BYTES + 1), raw=True)
    call = Mock(return_value=huge)
    monkeypatch.setattr(vendor.requests, 'request', call)
    assert vendor.fetch_a_share_attention('600941.SS', TODAY, TODAY).startswith('DATA_UNAVAILABLE')
    assert huge.close.call_count == 2


def test_total_deadline_stops_additional_network_work(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: elapsed[0])
    def slow_response(*args, **kwargs):
        elapsed[0] += 50
        return response(listing([row()]), raw=True)
    call = Mock(side_effect=slow_response)
    monkeypatch.setattr(vendor.requests, 'request', call)
    out = vendor.fetch_a_share_discussions('600941.SS', TODAY, TODAY)
    assert out.startswith('DATA_UNAVAILABLE')
    assert call.call_count == 1


def test_midnight_start_boundary_and_nested_forum_code_are_accepted(monkeypatch):
    a = {**row(), 'post_publish_time': '2026-11-04 00:00:00', 'post_guba': {'stockbar_code': '600941'}}
    del a['stockbar_code']
    fetch(monkeypatch, [listing([a]), listing([]), detail(a)])
    out = vendor.fetch_a_share_discussions('600941.SS', '2026-11-04', TODAY)
    assert not out.startswith('DATA_UNAVAILABLE')
    assert '2026-11-04T00:00:00+08:00' in out
