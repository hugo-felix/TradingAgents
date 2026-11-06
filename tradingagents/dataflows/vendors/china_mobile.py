"""Bounded, attributed China Mobile operating history from official disclosures.

Current research only: mutable official URLs cannot establish historical knowledge.
No derived growth rates, annualised capex, or chart-number reconstruction.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
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
from tradingagents.dataflows.vendors import cninfo

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 200
MAX_TEXT_CHARS = 300_000
CACHE_TTL_SECONDS = 6 * 60 * 60
MAX_OUTPUT_CHARS = 20_000


def _today():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date()


def _official_pdf_pages(client, url):
    """Download only the official HTTPS host, with bounded, expiring text cache."""
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'www.chinamobileltd.com'
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise VendorUnavailableError('China Mobile PDF URL is outside the allowed HTTPS host')
    path = Path(get_config()['data_cache_dir']) / 'china-mobile' / 'text-v2' / (hashlib.sha256(url.encode()).hexdigest() + '.json')
    now = time.time()
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
        pages = cached['pages']
        if (0 <= now - float(cached['fetched_epoch']) < CACHE_TTL_SECONDS
                and isinstance(pages, list) and len(pages) <= MAX_PDF_PAGES + 1
                and all(isinstance(p, str) for p in pages)
                and sum(map(len, pages)) <= MAX_TEXT_CHARS + len(PDF_TRUNCATION)):
            return pages
    except (OSError, ValueError, KeyError, TypeError):
        pass
    pages = download_pdf_pages(client, url, source='China Mobile', max_bytes=MAX_PDF_BYTES,
                               max_pages=MAX_PDF_PAGES, max_text_chars=MAX_TEXT_CHARS)
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda tmp: Path(tmp).write_text(json.dumps({'fetched_epoch': now, 'pages': pages}, ensure_ascii=False), encoding='utf-8'))
    return pages


def _interim_metrics(year, pages, *, facts=None, source_url=None):
    """Accept narrative actuals only, after verifying report issuer and year."""
    cover = ' '.join(pages[:2])
    if (not re.search(r'China\s*Mobile\s*Limited', cover, re.I)
            or not re.search(rf'Interim\s*Report\s*{year}\b', cover, re.I)):
        return []
    patterns = [
        ('实际资本开支', rf'In\s+the\s+first\s+half\s+of\s+{year},\s*our\s+capital\s+expenditure\s+was\s+RMB\s*([\d,]+(?:\.\d+)?)\s*billion', 'RMB billion'),
        ('移动ARPU', r'\bMobile\s+ARPU\b(?:[^.;]|\.(?=\d)){0,220}?\b(?:RMB)\s*([\d,]+(?:\.\d+)?)', 'RMB/user/month'),
        ('家庭客户综合ARPU', r'Household\s+customer\s+blended\s+ARPU\b(?:[^.;]|\.(?=\d)){0,180}?\bRMB\s*([\d,]+(?:\.\d+)?)', 'RMB/user/month'),
    ]
    lines = []
    for label, pattern, unit in patterns:
        for number, text in enumerate(pages[:MAX_PDF_PAGES], 1):
            match = re.search(pattern, text, re.I)
            if match:
                if facts is not None:
                    facts.append(Fact(key=f'mobile:{year}H1:{label}:actual', label=label,
                                      value=Decimal(match[1].replace(',', '')), unit=unit, currency='CNY',
                                      period=f'{year}H1', basis='H1 reported actual', source_url=source_url,
                                      qualifiers=(f'PDF physical page {number}; publication_date=unknown',
                                                  'Actual capex is not annualized, a full-year plan, commitment or cash payment',
                                                  'ARPU is per user per month; VAT and classification changes prevent verified cross-period comparability')))
                lines.append(f'{year}H1 {label}：{match[1]} {unit}（PDF物理第{number}页；原文：{match[0]}）')
                break
    return lines


def _quarter(title):
    if '摘要' in title or '客户' not in title:
        return None
    match = re.search(r'(20\d{2})\s*年\s*第([一二三四1234])季度', title)
    if not match:
        return None
    return int(match[1]), '一二三四'.index(match[2]) + 1 if match[2] in '一二三四' else int(match[2])


def _select_customers(entries, end):
    selected, seen = [], set()
    for row in sorted(entries, key=lambda r: (_quarter(r['title']) or (0, 0), r['day']), reverse=True):
        period = _quarter(row['title'])
        if not period or period in seen or row['day'] > end:
            continue
        year, quarter = period
        # First day of following quarter minus one is the reporting end.
        period_end = date(year + (quarter == 4), quarter * 3 % 12 + 1, 1) - timedelta(days=1)
        if period_end > end or row['day'] < period_end:
            continue
        seen.add(period)
        selected.append(row)
        if len(selected) == 5:
            break
    return selected


def _customer_metrics(title, pages, *, facts=None, source_url=None):
    period = _quarter(title)
    if not period:
        return []
    year, quarter = period
    identity = '\n'.join(pages[:2])
    if not re.search(r'中国移动有限公司', identity) or not re.search(r'600941', identity):
        return []
    # Require the actual PDF title's period, rather than trusting query metadata.
    if _quarter(identity) != period:
        return []
    patterns = [
        ('移动客户数', r'(?:移动客户数|移动业务\s*客户总数)\s+([\d,]+(?:\.\d+)?)'),
        ('5G网络客户数', r'5G\s*网络客户数\s+([\d,]+(?:\.\d+)?)'),
        ('有线宽带客户数', r'有线宽带业务\s*客户总数\s+([\d,]+(?:\.\d+)?)'),
        ('宽带联网客户数', r'宽带联网\s*1?\s*客户数\s+([\d,]+(?:\.\d+)?)'),
        ('物联网卡连接数', r'物联网卡连接数\s+([\d,]+(?:\.\d+)?)'),
    ]
    lines = []
    for label, pattern in patterns:
        for number, text in enumerate(pages[:MAX_PDF_PAGES], 1):
            if not re.search(r'单位\s*[：:]?\s*万户', text):
                continue
            match = re.search(pattern, text)
            if match:
                if facts is not None:
                    basis = 'period-end connections' if label == '物联网卡连接数' else 'period-end customers'
                    facts.append(Fact(key=f'mobile:{year}Q{quarter}:{label}:period_end', label=label,
                                      value=Decimal(match[1].replace(',', '')), unit='万户', period=f'{year}Q{quarter}',
                                      basis=basis, source_url=source_url,
                                      qualifiers=(f'PDF physical page {number}; internal operating statistics',
                                                  '5G network customers are not 5G package customers',
                                                  'Wireline broadband and expanded broadband connections are separate scopes; expanded scope includes home/enterprise broadband and Internet/data leased lines')))
                lines.append(f'{year}Q{quarter} {label}：{match[1]} 万户（期末；PDF物理第{number}页；原文：{match[0]}）')
                if label == '物联网卡连接数':
                    # Scope the net additions to this metric's block, never another business above it.
                    block = text[match.end():]
                    for suffix, flow_label, flow_pattern in (
                        ('quarter_net', 'quarterly net additions', r'本季度净增连接数\s+(-?[\d,]+(?:\.\d+)?)'),
                        ('ytd_net', 'year-to-date net additions', r'本年累计净增连接数\s+(-?[\d,]+(?:\.\d+)?)'),
                    ):
                        flow = re.search(flow_pattern, block)
                        if flow:
                            if facts is not None:
                                facts.append(Fact(key=f'mobile:{year}Q{quarter}:{label}:{suffix}', label=label,
                                                  value=Decimal(flow[1].replace(',', '')), unit='万户', period=f'{year}Q{quarter}',
                                                  basis=flow_label, source_url=source_url,
                                                  qualifiers=(f'PDF physical page {number}; internal operating statistics',
                                                              'IoT card connections; net additions are flows, not period-end stock')))
                            lines.append(f'{year}Q{quarter} {label}：{flow[1]} 万户（{flow_label}；PDF物理第{number}页；原文：{flow[0]}）')
                break
    return lines


def get_company_operating_history_result(ticker: str, as_of_date: str | None = None) -> SourceResult:
    """Current-only, bounded China Mobile H1 actuals and quarterly customers."""
    canonical = normalize_symbol(ticker)
    if not is_a_share(ticker) or canonical != '600941.SS':
        raise NoMarketDataError(ticker, detail='operating history only supports qualified China Mobile A-share 600941.SH/SS')
    end = date.fromisoformat(as_of_date) if as_of_date else _today()
    if end != _today():
        return SourceResult('DATA_UNAVAILABLE: withheld: 历史或未来as_of_date不请求当前官网；'
                '当前可修订PDF不能证明过去可得数据，未来日期亦不可用。')
    fetched_at = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    lines = [f'中国移动经营历史；as_of_date={end}；fetched_at={fetched_at}；当前取得的官方披露，非历史时点数据。',
             '范围：最近最多3个H1、最近5个唯一季度客户公告。fetched_at为本次来源读取时刻，官网正文可能来自最长6小时文本缓存，非出版时刻。H1实际资本开支不年化，不等于资本承诺、现金支付或全年计划。',
             'ARPU为同名原始披露值，2026 VAT税目政策及业务分类调整；未验证调整后可比ARPU，不计算跨期ARPU同比。',
             '有线宽带客户数与宽带联网客户数分别列示，不拼接、不计算跨口径同比；后者包括家庭宽带、企业宽带、互联网专线和数据专线。5G网络客户不是5G套餐客户。',
             f'missing: {end.year}全年数值资本开支计划未验证；publication_date=unknown的官网报告不以董事长/业绩日期冒充上传/出版日。']
    facts = [Fact(key=f'mobile:{end.year}:annual_capex_plan', label='全年资本开支计划', value=None,
                  unit='RMB billion', currency='CNY', period=str(end.year), basis='annual capex guidance',
                  status='unverified', qualifiers=('Not reliably extracted from the bounded sources; this does not establish nondisclosure',))]
    valid = 0
    client = requests.Session()
    client.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': cninfo.BASE + '/'})
    try:
        # Before H1 ends, the most recent completed H1 belongs to last year.
        latest = end.year if end >= date(end.year, 6, 30) else end.year - 1
        for year in range(latest, latest - 3, -1):
            url = f'https://www.chinamobileltd.com/en/ir/reports/ir{year}.pdf'
            lines.append(f'来源：{year}H1 official interim report；URL={url}；publication_date=unknown；fetched_at={fetched_at}')
            try:
                pages = _official_pdf_pages(client, url)
                if PDF_TRUNCATION in pages:
                    lines.append(PDF_TRUNCATION)
                metrics = _interim_metrics(year, pages, facts=facts, source_url=url)
                valid += len(metrics)
                lines.extend(metrics)
                for label in ('实际资本开支', '移动ARPU', '家庭客户综合ARPU'):
                    if not any(label in metric for metric in metrics):
                        lines.append(f'missing: {year}H1 {label}未可靠提取（空PDF/源错期/版式或未披露）')
            except (requests.RequestException, VendorUnavailableError, VendorNotConfiguredError, ValueError, OSError) as exc:
                lines.append(f'missing: {year}H1 source unavailable ({type(exc).__name__})')
        try:
            entries, truncated = cninfo._announcements(client, '600941', end - timedelta(days=1095), end, 'sse', keyword='客户')
            selected = _select_customers(entries, end)
            if not selected:
                lines.append('missing: 未找到可验证季度客户公告')
            if truncated:
                lines.append('missing: CNInfo查询达到页数上限，季度索引可能不完整')
            for row in selected:
                lines.append(f'来源：{row["title"]}；公告日={row["day"]}；URL={row["url"]}；fetched_at={fetched_at}')
                try:
                    pages = cninfo._pdf_pages(client, row['url'])
                    if PDF_TRUNCATION in pages:
                        lines.append(PDF_TRUNCATION)
                    metrics = _customer_metrics(row['title'], pages, facts=facts, source_url=row['url'])
                    valid += len(metrics)
                    lines.extend(metrics)
                    for label in ('移动客户数', '5G网络客户数'):
                        if not any(label in metric for metric in metrics):
                            lines.append(f'missing: {row["title"]} {label}未可靠提取')
                    if not any('宽带' in metric for metric in metrics):
                        lines.append(f'missing: {row["title"]} 宽带客户未可靠提取')
                except (requests.RequestException, VendorUnavailableError, VendorNotConfiguredError, ValueError, OSError) as exc:
                    lines.append(f'missing: 客户PDF不可用 ({type(exc).__name__})')
        except (requests.RequestException, NoMarketDataError, VendorUnavailableError, VendorNotConfiguredError, KeyError, TypeError, ValueError, OSError) as exc:
            lines.append(f'missing: CNInfo季度客户查询不可用 ({type(exc).__name__})')
    finally:
        client.close()
    if not valid:
        lines.insert(0, 'DATA_UNAVAILABLE: 所有来源均无有效指标，不能据此作经营判断。')
    result = '\n'.join(lines)
    content = result if len(result) <= MAX_OUTPUT_CHARS else result[:MAX_OUTPUT_CHARS - 40] + '\n[输出达到长度上限；其余来源省略]'
    return SourceResult(content, tuple(facts))


def get_company_operating_history(ticker: str, as_of_date: str | None = None) -> str:
    return get_company_operating_history_result(ticker, as_of_date).content
