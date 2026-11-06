"""Bounded, publication-date-aware CNInfo disclosure retrieval."""
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors import cninfo


def entry(title, day='2026-10-20', path=None, code='600941'):
    return {'secCode': code, 'orgId': 'org', 'announcementTitle': title,
            'announcementTime': int(datetime.fromisoformat(day).timestamp() * 1000),
            'adjunctUrl': path or f'finalpage/{day}/{title}.PDF'}


class Response:
    def __init__(self, data=None, content=b'%PDF fake', status=200):
        self.data, self.content, self.status_code = data, content, status
        self.headers = {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cninfo.requests.HTTPError('failure')

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        pass


@pytest.fixture
def source(monkeypatch, tmp_path):
    calls, rows = [], []
    class Client:
        def __init__(self):
            self.headers = {}
        def get(self, url, **kwargs):
            calls.append(('get', url, kwargs))
            if url.endswith('szse_stock.json'):
                return Response({'stockList': [{'code': '600941', 'orgId': 'org'}]})
            return Response()
        def post(self, url, **kwargs):
            calls.append(('post', url, kwargs))
            return Response({'announcements': rows, 'hasMore': False})
        def close(self):
            pass
    monkeypatch.setattr(cninfo.requests, 'Session', Client)
    monkeypatch.setattr(cninfo, 'get_config', lambda: {'data_cache_dir': str(tmp_path)})
    return rows, calls


def test_non_a_share_never_requests(source):
    with pytest.raises(NoMarketDataError):
        cninfo.get_company_disclosures('AAPL')
    assert source[1] == []


def test_public_diverse_selection_date_filter_dedupe_and_metrics(source, monkeypatch):
    rows, calls = source
    rows.extend([
        entry('2026年第三季度客户数据公告'),
        entry('2026年第二季度客户数据公告', '2026-07-20'),
        entry('2026年中期A股分红派息实施公告', '2026-08-27'),
        entry('2026年半年度报告', '2026-08-14'),
        entry('2026年第三季度报告', '2026-10-21'),
        entry('2026年第三季度报告摘要', '2026-10-21'),
        entry('未来公告', '2026-12-01'),
        entry('异公司', code='600000'),
    ])
    texts = {
        '2026年第三季度客户数据公告': ['2026年第三季度 单位：万户 移动客户数 101,405.5\n5G 网络客户数 70,228.2\n宽带联网 1 客户数 34,226.5'],
        '2026年半年度报告': ['2026年上半年 移动 ARPU 为人民币 45.1 元；家庭客户综合 ARPU 为人民币 46.8 元。2026 年派息率将稳中有升。资本开支610584'],
    }
    selected = []
    def pages(client, url):
        title = url.rsplit('/', 1)[-1][:-4]
        selected.append(title)
        return texts.get(title, ['营业收入与现金分红实施原文'])
    monkeypatch.setattr(cninfo, '_pdf_pages', pages)
    result = cninfo.get_company_disclosures('600941.SH', '2026-11-05', limit=4)
    assert set(selected) == {'2026年第三季度客户数据公告', '2026年中期A股分红派息实施公告', '2026年半年度报告', '2026年第三季度报告'}
    for text in ('101,405.5', '70,228.2', '34,226.5', '万户', '2026H1', '45.1', '46.8', '公司指引', '非已实现', '资本开支：未可靠提取', '第1页'):
        assert text in result
    assert '未来公告' not in result and '异公司' not in result
    assert '完整修订历史' in result
    query = [c for c in calls if c[0] == 'post'][0][2]['data']
    assert query['stock'] == '600941,org'
    assert query['seDate'].endswith('~2026-11-05')


def test_empty_filtered_results(source):
    source[0].append(entry('未来', '2026-12-01'))
    with pytest.raises(NoMarketDataError):
        cninfo.get_company_disclosures('600941.SS', '2026-11-05')


def test_bad_url_rejected_without_download(source):
    client = cninfo.requests.Session()
    for url in ('https://evil.com/a.pdf', 'http://static.cninfo.com.cn/a.pdf', 'https://static.cninfo.com.cn@evil.com/a.pdf'):
        with pytest.raises(VendorUnavailableError):
            cninfo._pdf_pages(client, url)
    assert not source[1]


def test_pdf_is_cached_with_bounded_pages(source, monkeypatch):
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=lambda _: SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: '原文') for _ in range(3)])))
    client = cninfo.requests.Session()
    url = 'https://static.cninfo.com.cn/finalpage/test.PDF'
    assert cninfo._pdf_pages(client, url) == ['原文'] * 3
    assert cninfo._pdf_pages(client, url) == ['原文'] * 3
    assert len(source[1]) == 1
    assert source[1][0][2]['allow_redirects'] is False


def test_redirect_and_oversized_pdf_rejected(source, monkeypatch):
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=lambda _: None))
    client = cninfo.requests.Session()
    client.get = lambda *a, **k: Response(status=302)
    with pytest.raises(VendorUnavailableError):
        cninfo._pdf_pages(client, 'https://static.cninfo.com.cn/a.PDF')
    client.get = lambda *a, **k: Response(content=b'%PDF' + b'x' * 20)
    monkeypatch.setattr(cninfo, 'MAX_PDF_BYTES', 10)
    with pytest.raises(VendorUnavailableError):
        cninfo._pdf_pages(client, 'https://static.cninfo.com.cn/a.PDF')


def test_missing_pypdf_is_configuration_error(source, monkeypatch):
    monkeypatch.setitem(sys.modules, 'pypdf', None)
    with pytest.raises(VendorNotConfiguredError):
        cninfo._pdf_pages(cninfo.requests.Session(), 'https://static.cninfo.com.cn/a.PDF')


def test_image_pdf_has_no_invented_metrics(source, monkeypatch):
    source[0].append(entry('2026年第三季度报告'))
    monkeypatch.setattr(cninfo, '_pdf_pages', lambda *args: [''])
    result = cninfo.get_company_disclosures('600941.SS', '2026-11-05')
    assert '无可读文字' in result
    assert '45.1' not in result


@pytest.mark.parametrize('pages', [[], [''], ['  \n'], ['[PDF提取截断：达到页数、文字或时间上限]']])
def test_unreadable_pdfs_preserve_index_but_cannot_bypass_review(source, monkeypatch, pages):
    from langchain_core.messages import ToolMessage

    from tradingagents.agents.evidence import collect_tool_evidence
    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
    from tradingagents.graph.propagation import Propagator

    source[0].append(entry('2026年第三季度报告'))
    monkeypatch.setattr(cninfo, '_pdf_pages', lambda *args: pages)
    result = cninfo.get_company_disclosures('600941.SS', '2026-11-05')
    assert result.startswith('DATA_UNAVAILABLE:')
    assert '2026年第三季度报告' in result and 'https://static.cninfo.com.cn/' in result
    assert '无可读文字' in result
    if cninfo.PDF_TRUNCATION in pages:
        assert cninfo.PDF_TRUNCATION in result
    current = Propagator().create_initial_state('600941.SS', '2026-11-05')
    current['fundamentals_evidence'] = collect_tool_evidence('fundamentals', [
        ToolMessage(result, tool_call_id='disclosures', name='get_company_disclosures'),
    ])
    assert next(iter(current['fundamentals_evidence'].values()))['status'] == 'unavailable'
    llm = MagicMock()
    decision = create_portfolio_manager(llm)(current)
    assert decision['final_rating'] == 'REVIEW'
    llm.invoke.assert_not_called()
    llm.with_structured_output.return_value.invoke.assert_not_called()


def test_readable_and_image_pdfs_keep_partial_source_available(source, monkeypatch):
    from langchain_core.messages import ToolMessage

    from tradingagents.agents.evidence import collect_tool_evidence

    source[0].extend([entry('2026年第三季度报告'), entry('2026年半年度报告')])
    monkeypatch.setattr(cninfo, '_pdf_pages', lambda client, url:
                        ['营业收入原文'] if '第三季度报告' in url else [''])
    result = cninfo.get_company_disclosures('600941.SS', '2026-11-05')
    assert '营业收入原文' in result and '无可读文字' in result
    assert '正文覆盖：1/2' in result
    evidence = collect_tool_evidence('fundamentals', [
        ToolMessage(result, tool_call_id='disclosures', name='get_company_disclosures'),
    ])
    assert next(iter(evidence.values()))['status'] == 'available'


def test_query_has_bounded_pagination_and_marks_truncation(source, monkeypatch):
    rows, calls = source
    rows.append(entry('2026年第三季度报告'))
    original_post = cninfo.requests.Session.post
    def post(self, *args, **kwargs):
        response = original_post(self, *args, **kwargs)
        response.data['hasMore'] = True
        return response
    monkeypatch.setattr(cninfo.requests.Session, 'post', post)
    monkeypatch.setattr(cninfo, '_pdf_pages', lambda *args: ['营业收入原文'])
    result = cninfo.get_company_disclosures('600941.SS', '2026-11-05')
    assert len([call for call in calls if call[0] == 'post']) == cninfo.MAX_QUERY_PAGES
    assert '公告查询截断：是' in result
    assert result.count('### 2026年第三季度报告') == 1


def test_operating_excerpts_do_not_infer_metrics_for_any_issuer():
    pages = ['移动客户数 101,405.5 5G网络客户数 70,228.2 宽带联网客户数 34,226.5']
    facts = []
    customer = {'title': '2026年第三季度客户数据公告', 'day': '2026-10-21', 'url': 'https://static.cninfo.com.cn/a.PDF'}
    cninfo._document(customer, pages, '600941', facts=facts)
    assert facts == []
    row = {'title': '2026年半年度报告', 'day': '2026-08-14', 'url': 'https://static.cninfo.com.cn/a.PDF'}
    result = cninfo._document(row, ['移动 ARPU 为人民币 45.1 元'], '600000')
    assert '未执行该公司的通用经营数值解析' in result
    assert '2026H1 移动ARPU' not in result


def test_page_and_output_boundaries(source, monkeypatch):
    monkeypatch.setattr(cninfo, 'MAX_PDF_PAGES', 2)
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=lambda _: SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: '原文') for _ in range(3)])))
    pages = cninfo._pdf_pages(cninfo.requests.Session(), 'https://static.cninfo.com.cn/pages.PDF')
    assert pages[:2] == ['原文', '原文']
    assert '截断' in pages[-1]
    assert cninfo._pdf_pages(cninfo.requests.Session(), 'https://static.cninfo.com.cn/pages.PDF') == pages
    assert len(source[1]) == 1
    source[0].append(entry('2026年第三季度报告'))
    monkeypatch.setattr(cninfo, '_pdf_pages', lambda *args: ['营业收入' + '长' * 10000])
    monkeypatch.setattr(cninfo, 'MAX_OUTPUT_CHARS', 500)
    output = cninfo.get_company_disclosures('600941.SS', '2026-11-05')
    assert len(output) <= 500 and '输出截断' in output


def test_network_failure_is_vendor_unavailable(source, monkeypatch):
    def fail(*args, **kwargs):
        raise cninfo.requests.Timeout('timeout')
    monkeypatch.setattr(cninfo.requests.Session, 'get', fail)
    with pytest.raises(VendorUnavailableError):
        cninfo.get_company_disclosures('600941.SS', '2026-11-05')


def test_recent_index_includes_uncategorized_material_events_and_caps_twenty(source, monkeypatch):
    rows, _ = source
    rows.extend(entry(f'重大合同公告{i}', '2026-10-22') for i in range(25))
    rows.append(entry('2026年第三季度报告', '2026-10-21'))
    rows.append(entry('未来重大事项', '2026-12-01'))
    monkeypatch.setattr(cninfo, '_pdf_pages', lambda *args: ['营业收入原文'])
    output = cninfo.get_company_disclosures('600941.SS', '2026-11-05')
    index_rows = [line for line in output.splitlines() if line.startswith('- ')]
    assert len(index_rows) == 20
    assert '重大合同公告0' in index_rows[0]
    assert '重大合同公告20 ' not in output
    assert '未来重大事项' not in output
    assert '索引不代表全文已读取' in output


def test_missing_dependency_message_names_existing_extra(source, monkeypatch):
    monkeypatch.setitem(sys.modules, 'pypdf', None)
    with pytest.raises(VendorNotConfiguredError, match=r"pip install '\.\[a-shares\]'"):
        cninfo._pdf_pages(cninfo.requests.Session(), 'https://static.cninfo.com.cn/a.PDF')


@pytest.mark.parametrize(('title', 'body'), [
    ('2026年第三季度客户数据公告', '单位：万户 移动客户数 101,405.5\n5G网络客户数 70,228.2'),
    ('2026年半年度报告', '移动 ARPU 为人民币 45.1 元；家庭客户综合 ARPU 为人民币 46.8 元。'),
])
def test_operating_numbers_remain_original_excerpts_without_duplicate_typed_facts(title, body):
    facts = []
    row = {'title': title, 'day': '2026-10-21', 'url': 'https://static.cninfo.com.cn/a.pdf'}
    result = cninfo._document(row, [body], '600941', facts=facts)
    assert facts == []
    assert body.split('；')[0] in result
    assert '原文选摘' in result
