"""Bounded excerpts from CNInfo's public website (not a guaranteed stable API).

China Mobile's verified dividend layout receives typed extraction. Operating
metrics belong to china_mobile; this source retains original disclosure context.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.facts import Fact, SourceResult
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.pdf import PDF_TRUNCATION, download_pdf_pages
from tradingagents.dataflows.symbols import is_a_share, normalize_symbol

BASE = 'https://www.cninfo.com.cn'
STATIC = 'https://static.cninfo.com.cn/'
MAX_QUERY_PAGES = 5
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_TEXT_CHARS = 300_000
MAX_OUTPUT_CHARS = 28_000
KEYWORDS = ('营业收入', '净利润', '经营活动', '移动客户', '5G', '宽带', 'ARPU', '分红', '派息', '每股', '股权登记日', '除权除息日')


def _pdf_url(value: str) -> str:
    url = urljoin(STATIC, value)
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'static.cninfo.com.cn'
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise VendorUnavailableError('CNInfo PDF URL is outside the allowed HTTPS host')
    return url


def _pdf_pages(client, url: str) -> list[str]:
    """Cache bounded extracted pages; never follow a PDF redirect."""
    url = _pdf_url(url)
    cache = Path(get_config()['data_cache_dir']) / 'cninfo' / 'text-v1'
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (hashlib.sha256(url.encode()).hexdigest() + '.json')
    try:
        pages = json.loads(path.read_text(encoding='utf-8'))
        if (isinstance(pages, list) and len(pages) <= MAX_PDF_PAGES + 1
                and all(isinstance(p, str) for p in pages)
                and sum(map(len, pages)) <= MAX_TEXT_CHARS + len(PDF_TRUNCATION)):
            return pages
    except (OSError, ValueError):
        pass
    pages = download_pdf_pages(client, url, source='CNInfo', max_bytes=MAX_PDF_BYTES,
                               max_pages=MAX_PDF_PAGES, max_text_chars=MAX_TEXT_CHARS)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(pages, ensure_ascii=False), encoding='utf-8'))
    return pages


def _publication_day(value) -> date | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000, ZoneInfo('Asia/Shanghai')).date()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _announcements(client, code, start, end, column, keyword=''):
    response = client.get(BASE + '/new/data/szse_stock.json', timeout=(10, 20))
    response.raise_for_status()
    matches = [s for s in response.json()['stockList'] if s.get('code') == code]
    if len(matches) != 1 or not matches[0].get('orgId'):
        raise NoMarketDataError(code, detail='CNInfo stock code did not uniquely resolve to an orgId')
    org = matches[0]['orgId']
    params = {'pageSize': 30, 'column': column, 'tabName': 'fulltext',
              'stock': f'{code},{org}', 'searchkey': keyword, 'secid': '', 'plate': '',
              'category': '', 'trade': '', 'seDate': f'{start}~{end}',
              'sortName': 'time', 'sortType': 'desc', 'isHLtitle': 'false'}
    entries, seen, truncated = [], set(), False
    for page in range(1, MAX_QUERY_PAGES + 1):
        response = client.post(BASE + '/new/hisAnnouncement/query', data={**params, 'pageNum': page}, timeout=(10, 20))
        response.raise_for_status()
        data = response.json()
        for row in (data.get('announcements') or [])[:30]:
            day = _publication_day(row.get('announcementTime'))
            if (row.get('secCode') != code or row.get('orgId') != org
                    or day is None or not start <= day <= end):
                continue
            try:
                url = _pdf_url(row.get('adjunctUrl') or '')
            except (VendorUnavailableError, ValueError):
                continue
            if not url.lower().split('?', 1)[0].endswith('.pdf') or url in seen:
                continue
            seen.add(url)
            title = re.sub('<[^>]+>', '', str(row.get('announcementTitle') or ''))[:250]
            entries.append({'title': title, 'day': day, 'url': url})
        truncated = bool(data.get('hasMore'))
        if not truncated:
            break
    return sorted(entries, key=lambda r: r['day'], reverse=True), truncated


def _category(title):
    if '摘要' in title:
        return None
    if re.search('客户|运营数据|营运数据|经营数据', title):
        return 'customers'
    if '实施' in title and re.search('分红|分派|派息', title):
        return 'dividend'
    if '报告' in title and re.search('半年度|中期', title):
        return 'interim'
    if '报告' in title and re.search('第[一三]季度|[一三]季度', title):
        return 'quarter'
    if '年度报告' in title:
        return 'annual'
    return None


def _select(entries, limit):
    selected = []
    categories = ('customers', 'dividend', 'interim', 'quarter', 'annual')
    if limit >= 6:
        # Reserve the six default slots for evidence across periods and categories.
        for category in ('customers', 'interim'):
            candidates = [r for r in entries if _category(r['title']) == category]
            if not candidates:
                continue
            latest = candidates[0]
            selected.append(latest)
            year = re.search(r'20\d{2}', latest['title'])
            comparable = None
            if year:
                previous_title = (latest['title'][:year.start()] + str(int(year[0]) - 1)
                                  + latest['title'][year.end():])
                comparable = next((r for r in candidates[1:] if r['title'] == previous_title), None)
            if comparable is None and category == 'customers' and len(candidates) > 1:
                comparable = candidates[1]
            if comparable:
                selected.append(comparable)
        categories = ('dividend', 'quarter', 'annual')
    for category in categories:
        row = next((r for r in entries if _category(r['title']) == category), None)
        if row and row not in selected and len(selected) < limit:
            selected.append(row)
    for row in entries:
        if len(selected) >= limit:
            break
        if limit >= 6 and _category(row['title']) is not None:
            continue
        if row not in selected and '摘要' not in row['title']:
            selected.append(row)
    return selected


def _mobile_context(title, pages):
    """Bounded original context, only for the verified Mobile interim layout.

    These are attributed excerpts, not inferred metrics or computed growth rates.
    Separate footnotes must survive even when the headline occurs much earlier.
    """
    interim = re.search(r'(20\d{2})\s*年.*(?:半年度|中期)', title)
    if not interim or _category(title) != 'interim':
        return []
    period = f'{interim[1]}H1'
    lines = [f'{period} 关键解释与经营原文（来源同本公告；收入不等于利润）：',
             '管理层同口径说明与原始报表同比并列；不据此改写报表同比。',
             '现金与分红口径：仅并列同期间原文；全体普通股拟宣派金额并非已支付，'
             '不与A股单独实施金额混同。不自动计算纯派息率、分红覆盖率或可持续结论；'
             '口径不足时不可比较，不将期间不明的TTM现金流当作本期。']

    def excerpt(label, pattern, after=500, before=40):
        for number, text in enumerate(pages, 1):
            match = re.search(pattern, text)
            if match:
                original = text[max(0, match.start() - before):match.end() + after].strip()
                lines.append(f'{period} {label}（第{number}页；原文，金额单位/期间以原文为准）：{original}')
                return number
        lines.append(f'{period} {label}：未可靠提取')
        return None

    for number, text in enumerate(pages, 1):
        guidance = re.search(rf'{interim[1]}\s*年\s*派息率[^。\n]{{0,80}}', text)
        if guidance:
            lines.append(f'公司指引（非已实现派息率）：{guidance[0]}（第{number}页）')
            break

    excerpt('营收与归母利润/报表同比', r'公司营业收入达到人民币', 690)
    excerpt('同口径定义（增值税税目）', r'同口径指剔除增值税税目政策调整影响', 90, 5)
    excerpt('通信服务收入', r'通信服务阶段承压', 320, 0)
    excerpt('算力服务收入', r'算力服务增势强劲', 300, 0)
    excerpt('智能服务收入', r'智能服务提能蓄势', 280, 0)
    # Table columns must explicitly identify H1 years; a bare FCF number is insufficient.
    cash_page = None
    year = interim[1]
    for number, text in enumerate(pages, 1):
        header = re.search(rf'{year}\s*年上半年\s+{int(year) - 1}\s*年上半年\s+变化', text)
        fcf = re.search(r'自由现金流\s+[\d,]+\s+[\d,]+\s+[\d.]+%', text)
        if not header or not fcf or fcf.start() < header.start():
            continue
        # The verified financial discussion declares units at its section opening.
        unit = None
        for unit_number in range(max(0, number - 3), number):
            unit_match = re.search(r'除特别注明外，金额单位为人民币百万元', pages[unit_number])
            if unit_match and ('公司主要会计数据和财务指标' in pages[unit_number]):
                unit = (unit_number + 1, unit_match[0])
        if unit:
            lines.append(f'{period} 现金流表（第{number}页；单位来源第{unit[0]}页原文：{unit[1]}）：'
                         + text[header.start():fcf.end()])
            cash_page = number
            break
    if cash_page is None:
        lines.append(f'{period} 自由现金流：未可靠提取明确单位及期间的可比表；'
                     '以下如有管理层亿元披露属于四舍五入口径。')
        excerpt('管理层自由现金流披露（四舍五入口径）', r'自由现金流为人民币', 100)
    excerpt('全体普通股拟宣派中期股息', r'拟宣派的中期股息合计人民币', 210, 630)
    for label, pattern in (
        ('应收账款账龄/坏账原文（未结构化）', r'应收账款自账单产生时计算账龄'),
        ('坏账准备原文（未结构化）', r'\(c\)\s*坏账准备'),
        ('受限资金原文（未结构化）', r'报告期末主要资产受限情况'),
        ('租赁负债原文（未结构化）', r'租赁负债\s+四\(29\)'),
    ):
        excerpt(label, pattern, 500, 100)
    lines.append('现金风险注释仅选摘原文，未完整结构化；应收增加本身不代表坏账。'
                 '本公告选摘的资本开支：未可靠提取；其他官方文字披露应单独核验；不读图表拼接数字，不用投资净现金流替代。')
    return lines


def _has_readable_text(pages):
    """Extraction notices and blank pages are not original PDF body text."""
    return any(text.strip() and text.strip() != PDF_TRUNCATION for text in pages)


def _document(row, pages, code, *, facts=None):
    lines = [f"\n### {row['title']}\n发布日期：{row['day']}\n来源：{row['url']}"]
    if not _has_readable_text(pages):
        if PDF_TRUNCATION in pages:
            lines.append(PDF_TRUNCATION)
        return '\n'.join(lines + ['PDF无可读文字，未执行OCR，经营指标缺失。'])
    if code == '600941':
        if facts is not None:
            facts.extend(_mobile_dividend_facts(row, pages))
        lines.extend(_mobile_context(row['title'], pages))
    else:
        lines.append('范围：仅提供以下公告原文选摘；未执行该公司的通用经营数值解析。')
    excerpts, used = [], set()
    for keyword in KEYWORDS:
        for number, text in enumerate(pages, 1):
            match = re.search(re.escape(keyword), text)
            if match:
                excerpt = text[max(0, match.start() - 35):match.end() + 160].strip()
                if excerpt not in used:
                    used.add(excerpt)
                    excerpts.append(f'第{number}页原文选摘（{keyword}）：{excerpt}')
                break
        if len(excerpts) >= 8:
            break
    if not excerpts:
        excerpts = [f'第{i}页原文选摘：{p[:600]}' for i, p in enumerate(pages, 1) if p.strip()][:1]
    lines.extend(excerpts)
    if any('PDF提取截断' in p for p in pages):
        lines.append('PDF提取截断：达到页数、文字或时间上限。')
    return '\n'.join(lines)


def get_company_disclosures_result(ticker: str, as_of_date: str | None = None,
                            look_back_days: int = 730, limit: int = 6) -> SourceResult:
    """Return dated original excerpts plus verified issuer-specific dividend facts.

    Publication dates filter the documents currently listed by CNInfo; this is
    not a complete revision history or a guarantee of historical availability.
    """
    canonical = normalize_symbol(ticker)
    if not is_a_share(canonical):
        raise NoMarketDataError(ticker, canonical, 'CNInfo supports exchange-qualified A-shares only')
    end = date.fromisoformat(as_of_date) if as_of_date else datetime.now(ZoneInfo('Asia/Shanghai')).date()
    if isinstance(look_back_days, bool) or not isinstance(look_back_days, int) or not 1 <= look_back_days <= 1095:
        raise ValueError('look_back_days must be an integer between 1 and 1095')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
        raise ValueError('limit must be an integer between 1 and 8')
    start, code = end - timedelta(days=look_back_days), canonical.split('.')[0]
    client = requests.Session()
    client.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': BASE + '/'})
    try:
        column = 'sse' if canonical.endswith('.SS') else 'bse' if canonical.endswith('.BJ') else 'szse'
        entries, truncated = _announcements(client, code, start, end, column)
        if not entries:
            raise NoMarketDataError(ticker, canonical, 'No CNInfo disclosures published in requested window')
        selected = _select(entries, limit)
        lines = [f'CNInfo正式披露与原文选摘：{canonical}；披露窗口 {start} 至 {end}',
                 '按发布日期筛选当前可查文档，非完整修订历史；未披露不代表数值为零。',
                 '来源为巨潮网页查询入口，非承诺稳定API；PDF页码为物理页码。',
                 '本工具所选公告的资本开支：未可靠提取；其他官方文字披露应单独核验；不将现金流当作资本开支，也不将图表拼接数字当作数值。',
                 f'公告查询截断：{"是（达到分页上限）" if truncated else "否"}',
                 '最近公告索引（最多20项，索引不代表全文已读取）：']
        lines.extend(f"- {r['day']} {r['title']} {r['url']}" for r in entries[:20])
        if code == '600941':
            customer_count = sum(_category(r['title']) == 'customers' for r in selected)
            interim_count = sum(_category(r['title']) == 'interim' for r in selected)
            lines.append('历史趋势：仅并列各期原文，口径不同不强算增长率。')
            if customer_count < 2:
                lines.append('客户历史对比：缺可比同期/前一期PDF。')
            if interim_count < 2:
                lines.append('中期历史对比：缺上年同期PDF。')
        facts = []
        retrieved, readable = 0, 0
        for row in selected:
            try:
                pages = _pdf_pages(client, row['url'])
                lines.append(_document(row, pages, code, facts=facts))
                retrieved += 1
                readable += _has_readable_text(pages)
            except VendorNotConfiguredError:
                raise
            except (VendorUnavailableError, requests.RequestException, ValueError, OSError) as exc:
                lines.append(f"{row['title']}（{row['day']}）：正文不可用（{type(exc).__name__}）；来源：{row['url']}")
        if not retrieved:
            raise VendorUnavailableError('CNInfo disclosures listed but no selected PDF could be retrieved')
        lines.insert(0, f'正文覆盖：{readable}/{len(selected)} 份所选PDF提取到可读文字；不代表完整解析。')
        if not readable:
            lines.insert(0, 'DATA_UNAVAILABLE: 所选公告PDF均无可读正文；以下仅保留公告索引和提取情况，不能据此认定正文证据可用。')
        result = '\n'.join(lines)
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS - 30] + '\n[输出截断：达到文字上限]'
        return SourceResult(result, tuple(facts))
    except (requests.RequestException, KeyError, TypeError, ValueError, OSError) as exc:
        if isinstance(exc, VendorNotConfiguredError):
            raise
        raise VendorUnavailableError(f'CNInfo website retrieval failed ({type(exc).__name__})') from exc
    finally:
        client.close()


def _mobile_dividend_facts(row, pages):
    """Parse only explicit issuer dividend denomination/growth expressions.

    Join page text because the RMB expression can cross a physical page break.
    Percentages remain attached to their own currency and are never recomputed.
    """
    if _category(row['title']) != 'dividend':
        return []
    text = '\n'.join(pages)
    if not re.search(r'中国移动有限公司', text) or not re.search(r'600941', text):
        return []
    period_match = re.search(r'截至\s*(20\d{2})\s*年\s*6\s*月\s*30\s*日止六个月', text)
    if not period_match:
        return []
    period = f'{period_match[1]}H1'
    patterns = (
        ('CNY', r'中期股息每股\s*([\d,.]+)\s*元人民币[（(]含税[）)]，\s*同比增长\s*([\d.]+)%'),
        ('HKD', r'港元股息\s*金额为每股\s*([\d,.]+)\s*港元[（(]含税[）)]，\s*同比增长\s*([\d.]+)%'),
    )
    facts = []
    for currency, pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        qualifiers = ('Currency denomination matters: CNY declared dividend growth and HKD translated payment growth are separate',
                      'Gross/pre-tax; A-share implementation is not total ordinary shareholder distributions',
                      'Explicit issuer expression; no inference of policy stagnation from converted per-share cash')
        for suffix, value, unit, label in (
            ('cash', match[1], f'{currency}/share', '中期每股股息'),
            ('growth', match[2], '%', '中期每股股息同比增长'),
        ):
            facts.append(Fact(key=f'cninfo:dividend:{period}:{currency}:{suffix}:{row["url"]}', label=label,
                              value=Decimal(value.replace(',', '')), unit=unit, currency=currency,
                              period=period, basis='declared RMB dividend' if currency == 'CNY' else 'translated HKD dividend',
                              qualifiers=qualifiers, source_url=row['url']))
    return facts


def get_company_disclosures(ticker: str, as_of_date: str | None = None,
                            look_back_days: int = 730, limit: int = 6) -> str:
    return get_company_disclosures_result(ticker, as_of_date, look_back_days, limit).content
