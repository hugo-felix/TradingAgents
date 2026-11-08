"""Current A-share income screen. No LLM calls or historical backtest claims."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from tradingagents.dataflows.dividend_financials import (
    INDICATORS,
    financial_checks,
    financial_sector,
)
from tradingagents.dataflows.dividend_market import (
    INDEX_FALLBACK,
    INDEX_HISTORY,
    SECTOR_HISTORY,
    collect_market_context,
)
from tradingagents.dataflows.errors import VendorUnavailableError

# A research seed pool, not a certified ownership/industry classification.
UNIVERSE = [
    ('600941', '中国移动', '通信'), ('601728', '中国电信', '通信'),
    ('600050', '中国联通', '通信'), ('601088', '中国神华', '煤炭'),
    ('601225', '陕西煤业', '煤炭'), ('600900', '长江电力', '水电'),
    ('600028', '中国石化', '石油石化'), ('601857', '中国石油', '石油石化'),
    ('600938', '中国海油', '油气开采'), ('601006', '大秦铁路', '铁路运输'),
    ('600019', '宝钢股份', '钢铁'), ('600036', '招商银行', '银行'),
    ('601398', '工商银行', '银行'), ('601939', '建设银行', '银行'),
    ('601288', '农业银行', '银行'), ('601988', '中国银行', '银行'),
]
ENDPOINTS = {
    'dividends': 'stock_fhps_detail_em',
    'cashflow': 'stock_cash_flow_sheet_by_report_em',
    'profit': 'stock_profit_sheet_by_report_em',
    'balance': 'stock_balance_sheet_by_report_em',
}
DISCOVERY = 'stock_zh_a_spot_em'
DISCOVERY_FALLBACK = 'stock_zh_a_spot_tx'
TRADE_CALENDAR = 'tool_trade_date_hist_sina'
HISTORY = 'stock_zh_a_hist'
PROFILE = 'stock_individual_info_em'
DIVIDENDS_FALLBACK = 'stock_dividend_cninfo'
ROSTER_ENDPOINTS = ('stock_info_sh_name_code', 'stock_info_sz_name_code',
                    'stock_info_bj_name_code')
RULES = '全A股发现，市值≥500亿元及市值缺失者进入核验；最近完整年度税前股息率>4%；连续5个完整年度现金分红；静态PE(最近年报)>0且≤20；近3年归母利润为正、最近年降幅≤30%；普通企业近3年经营现金流均为正、3年累计覆盖净利润，资产负债率≤75%；银行、保险、证券及多元金融改核行业监管指标，未取得时待核验。'
CORE_RULES = ('精选为研究预设，未经回测优化：基础量化通过；最近完整年度每股分红/基本每股收益在(0,80%]；'
              '最近3年每股分红每年降幅≤20%；1年及3年年化波动率均≤25%；'
              '1年最大回撤≤25%、3年最大回撤≤35%；20日日均成交额≥1亿元。'
              '资料缺失、送转导致派息率口径不可比或金融监管指标未核验时不入选。'
              '派息率为每股口径估算，不等同于实际现金分红总额/归母利润。')


def market_symbol(code):
    return ('SH' if code.startswith('6') else 'BJ' if code.startswith(('4', '8', '92')) else 'SZ') + code


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def worker(endpoint, symbol):
    # SDK calls run in a bounded child process: a hung provider cannot hang the desktop.
    from tradingagents.dataflows.vendors.akshare.common import request
    if endpoint == ROSTER_ENDPOINTS[0]:
        frame = request(endpoint, symbol='科创板' if symbol == 'star' else '主板A股')
    elif endpoint == ROSTER_ENDPOINTS[1]:
        frame = request(endpoint, symbol='A股列表')
    elif endpoint in (ROSTER_ENDPOINTS[2], DISCOVERY, DISCOVERY_FALLBACK, TRADE_CALENDAR):
        frame = request(endpoint)
    elif endpoint == INDICATORS:
        frame = request(endpoint, symbol=f'{symbol[2:]}.{symbol[:2]}', indicator='按报告期')
    elif endpoint in (HISTORY, INDEX_HISTORY, INDEX_FALLBACK, SECTOR_HISTORY):
        today = date.today()
        params = {'symbol': symbol, 'period': '日k' if endpoint == SECTOR_HISTORY else 'daily',
                  'start_date': (today - timedelta(days=1120)).strftime('%Y%m%d'),
                  'end_date': (today - timedelta(days=1)).strftime('%Y%m%d')}
        if endpoint == INDEX_FALLBACK:
            params.pop('period')
        elif endpoint != INDEX_HISTORY:
            params['adjust'] = 'qfq' if endpoint == HISTORY else ''
        frame = request(endpoint, **params)
    else:
        frame = request(endpoint, symbol=symbol)
    print(frame.to_json(orient='records', force_ascii=False, date_format='iso'))


def screen_command():
    if getattr(sys, 'frozen', False):
        executable = (str(Path(sys.executable).with_name('StockSeekWorker.exe'))
                      if os.name == 'nt' else sys.executable)
        return [executable, '--dividend-worker']
    return [sys.executable, '-m', 'tradingagents.dataflows.dividend_screen']


class Source:
    def __init__(self, directory, direct=False, timeout=50):
        self.directory = Path(directory)
        self.direct = direct
        self.timeout = timeout

    def fetch(self, endpoint, symbol):
        # Discovery/history must never reuse yesterday's window as today's scan.
        window = date.today().isoformat() if endpoint in (
            DISCOVERY, DISCOVERY_FALLBACK, HISTORY, INDEX_HISTORY, INDEX_FALLBACK, SECTOR_HISTORY,
            TRADE_CALENDAR, *ROSTER_ENDPOINTS) else ''
        key = hashlib.sha256(f'{endpoint}:{symbol}:{window}'.encode()).hexdigest()[:20]
        path = self.directory / f'{key}.json'
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding='utf-8'))
                if isinstance(cached.get('rows'), list) and cached['rows'] and 0 <= time.time() - cached['fetched_epoch'] < 86400:
                    return cached
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
        env = dict(os.environ, PYTHONUTF8='1')
        if self.direct:
            env['NO_PROXY'] = ','.join(filter(None, [env.get('NO_PROXY'), '.eastmoney.com',
                                                    'eastmoney.com', '.finance.qq.com',
                                                    'finance.qq.com', '.cninfo.com.cn',
                                                    '.sse.com.cn', '.szse.cn', '.bse.cn']))
            env['no_proxy'] = env['NO_PROXY']
        failures = []
        for attempt in range(2):
            try:
                result = subprocess.run(
                    [*screen_command(), '--worker', endpoint, symbol],
                    capture_output=True, encoding='utf-8', env=env, timeout=self.timeout,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True,
                )
                rows = json.loads(result.stdout)
                if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                    raise ValueError('invalid provider result')
                if not rows:
                    failures.append('返回空表（不代表没有分红）')
                    if attempt == 0:
                        time.sleep(1)
                    continue
                payload = {'endpoint': endpoint, 'symbol': symbol, 'fetched_epoch': time.time(),
                           'fetched_at': datetime.now().astimezone().isoformat(), 'rows': rows}
                atomic_json(path, payload)
                return payload
            except (subprocess.SubprocessError, ValueError, OSError) as exc:
                failures.append('请求超时' if isinstance(exc, subprocess.TimeoutExpired)
                                else '接口进程异常' if isinstance(exc, subprocess.SubprocessError)
                                else '返回格式无效' if isinstance(exc, ValueError)
                                else '进程启动失败')
                if attempt == 0:
                    time.sleep(1)
        # Do not silently reuse expired financial statements as current screening inputs.
        raise VendorUnavailableError(f'{endpoint}: ' + '；'.join(failures)
                                     + '；旧缓存未用于本轮筛选')

    def quotes(self, codes):
        import requests
        session = requests.Session()
        session.trust_env = not self.direct
        if len(codes) > 80:
            result = {}
            for offset in range(0, len(codes), 80):
                result.update(self.quotes(codes[offset:offset + 80]))
            return result
        if not codes:
            return {}
        symbols = [market_symbol(c).lower() for c in codes]
        for attempt in range(2):
            try:
                response = session.get('https://qt.gtimg.cn/q=' + ','.join(symbols), timeout=15)
                response.raise_for_status()
                response.encoding = 'gbk'
                result = {}
                for symbol, payload in re.findall(r'v_([a-z0-9]+)="(.*?)";', response.text, re.S):
                    fields = payload.split('~')
                    if len(fields) > 46 and fields[2] == symbol[2:]:
                        result[symbol[2:]] = {'price': number(fields[3]), 'quote_time': fields[30],
                                             'market_cap_yi': number(fields[45])}
                if not result:
                    raise ValueError('empty quotes')
                return result
            except (requests.RequestException, ValueError):
                if attempt == 0:
                    time.sleep(1)
        raise VendorUnavailableError('腾讯行情两次获取失败；保留上次完整快照')


def calendar_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def trading_calendar(source):
    """Keep the provider's actual coverage; never extrapolate future sessions."""
    try:
        packet = source.fetch(TRADE_CALENDAR, 'all')
        dates = sorted({day.isoformat() for row in packet['rows']
                        if (day := calendar_date(row.get('trade_date')))})
        dates = dates[-800:]
        if len(dates) < 200 or any(date.fromisoformat(day).weekday() > 4 for day in dates):
            raise ValueError('incomplete trading calendar')
        return {'status': 'available', 'source': TRADE_CALENDAR,
                'first_date': dates[0], 'last_date': dates[-1], 'dates': dates}
    except (VendorUnavailableError, ValueError, KeyError, TypeError):
        return {'status': 'unavailable', 'source': TRADE_CALENDAR}


def calendar_target(calendar, target):
    """Return a verified session only inside a coherent calendar's bounds."""
    try:
        if calendar.get('status') != 'available':
            return None
        days = [date.fromisoformat(day) for day in calendar['dates']]
        if (len(days) < 200 or days != sorted(set(days))
                or any(day.weekday() > 4 for day in days)
                or days[0].isoformat() != calendar['first_date']
                or days[-1].isoformat() != calendar['last_date']
                or not days[0] <= target <= days[-1]):
            return None
        return max(day for day in days if day <= target)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def cash_plan(row):
    """Recognize only explicit cash-only plans, including minority-holder rates."""
    description = str(row.get('现金分红-现金分红比例描述') or '').strip()
    amount = r'(\d+(?:\.\d+)?)'
    match = re.fullmatch(
        rf'10派{amount}元\(含税(?:,扣税后\d+(?:\.\d+)?元)?\)'
        rf'(?:\(中小股东10派{amount}元;大股东10派{amount}元\))?', description)
    if not match:
        return None
    base, minority, _ = match.groups()
    cash = number(row.get('source_cash_per10', row.get('现金分红-现金分红比例')))
    if cash is None or not math.isclose(float(base), cash, abs_tol=1e-8):
        return None
    return float(minority) if minority is not None else cash


def split_ratio(row):
    """Resolve a null ratio only from an explicit, matching cash-only plan."""
    value = number(row.get('送转股份-送转总比例'))
    if value is not None:
        return value
    if cash_plan(row) is not None:
        return 0.0
    return None


def cash_ratio(row):
    """Only infer zero cash from an explicit, matching pure bonus/transfer plan."""
    plan = cash_plan(row)
    if plan is not None:
        return plan
    description = str(row.get('现金分红-现金分红比例描述') or '').strip()
    if '中小股东' in description or '大股东' in description:
        # A weighted company-wide amount is not an ordinary shareholder's DPS.
        return None
    cash = number(row.get('现金分红-现金分红比例'))
    if cash is not None:
        return cash
    description = str(row.get('现金分红-现金分红比例描述') or '').strip()
    match = re.fullmatch(r'10(?:送|转)(\d+(?:\.\d+)?)(?:股)?', description)
    split = split_ratio(row)
    if match and split is not None and split > 0 and math.isclose(float(match[1]), split, abs_tol=1e-8):
        return 0.0
    return None


def cninfo_dividends(rows):
    """Map implemented CNINFO events; ratios are CNY/shares per ten shares."""
    periods = {'年报': '12-31', '半年报': '06-30', '中报': '06-30',
               '一季报': '03-31', '三季报': '09-30'}
    result = []
    for row in rows:
        match = re.fullmatch(r'(\d{4})(年报|半年报|中报|一季报|三季报)',
                             str(row.get('报告时间') or '').strip())
        bonus, transfer = number(row.get('送股比例')), number(row.get('转增比例'))
        result.append({
            '报告期': f'{match[1]}-{periods[match[2]]}' if match else None,
            '方案进度': '实施分配', '除权除息日': row.get('除权日'),
            '最新公告日期': row.get('实施方案公告日期'),
            '现金分红-现金分红比例': row.get('派息比例'),
            '现金分红-现金分红比例描述': row.get('实施方案分红说明'),
            '送转股份-送转总比例': bonus + transfer
            if bonus is not None and transfer is not None else None,
            'source_endpoint': DIVIDENDS_FALLBACK, 'source_record': row,
        })
    return result


def fetch_dividends(source, code):
    """Fall back on a separate source without merging incompatible events."""
    errors = []
    for endpoint in (ENDPOINTS['dividends'], DIVIDENDS_FALLBACK):
        try:
            packet = source.fetch(endpoint, code)
            rows = packet['rows'] if endpoint == ENDPOINTS['dividends'] else cninfo_dividends(packet['rows'])
            return rows, {k: v for k, v in packet.items() if k != 'rows'}, errors
        except VendorUnavailableError as exc:
            errors.append(str(exc))
    return [], {}, errors


def annual(rows, asof):
    valid = []
    for row in rows:
        report = calendar_date(row.get('REPORT_DATE'))
        notice = calendar_date(row.get('NOTICE_DATE'))
        if report and notice and report <= notice <= asof and (report.month, report.day) == (12, 31):
            valid.append(row)
    result = {}
    for row in sorted(valid, key=lambda r: str(r.get('NOTICE_DATE')), reverse=True):
        result.setdefault(str(row['REPORT_DATE'])[:4], row)
    return result


def discover(rows, codes=None):
    """Coarse market-cap screen, retaining unknown caps so missing data cannot hide stocks."""
    selected, seen, excluded, unknown = [], set(), 0, 0
    for row in rows:
        code, name = str(row.get('代码', '')), str(row.get('名称') or '').strip()
        if not re.fullmatch(r'\d{6}', code) or not name or code in seen:
            raise VendorUnavailableError('全市场行情代码/名称缺失或重复，未发布不完整扫描')
        seen.add(code)
        cap = number(row.get('总市值'))
        if cap is not None and cap > 0 and cap < 500e8:
            excluded += 1
            continue
        unknown += cap is None or cap <= 0
        if not codes or code in codes:
            selected.append((code, name, '待识别'))
    if codes and not set(codes) <= seen:
        raise ValueError('指定代码不在本轮全A股行情中')
    return selected, {'scope': 'subset' if codes else 'all_a', 'scanned': len(seen),
                      'excluded_small_cap': excluded, 'unknown_cap': unknown,
                      'candidate_codes': [row[0] for row in selected]}


def exchange_roster(source):
    """Current exchange lists, with per-board failures instead of a false full count."""
    boards = [(ROSTER_ENDPOINTS[0], 'all', 1000),
              (ROSTER_ENDPOINTS[0], 'star', 400),
              (ROSTER_ENDPOINTS[1], 'all', 2000),
              (ROSTER_ENDPOINTS[2], 'all', 200)]

    def load(board):
        endpoint, symbol, minimum = board
        try:
            packet = source.fetch(endpoint, symbol)
            rows = {}
            for row in packet['rows']:
                code = str(row.get('证券代码', row.get('A股代码', '')))
                name = str(row.get('证券简称', row.get('A股简称', '')) or '').strip()
                if not re.fullmatch(r'\d{6}', code) or not name or code in rows:
                    raise VendorUnavailableError('交易所名单代码或名称无效')
                rows[code] = name
            if len(rows) < minimum:
                raise VendorUnavailableError('交易所名单疑似不完整')
            return rows, {'endpoint': endpoint, 'board': symbol, 'status': 'available',
                          'count': len(rows), 'fetched_at': packet.get('fetched_at')}
        except VendorUnavailableError:
            return {}, {'endpoint': endpoint, 'board': symbol, 'status': 'unavailable',
                        'count': 0, 'fetched_at': None}

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(load, boards))
    names = {}
    for rows, _ in results:
        if names.keys() & rows.keys():
            raise VendorUnavailableError('交易所名单跨板块代码重复')
        names.update(rows)
    return {'status': 'confirmed' if all(b['status'] == 'available' for _, b in results)
            else 'unconfirmed', 'names': names, 'boards': [b for _, b in results]}


def discover_market(source, codes=None, roster=None):
    """Try an independent provider when the primary is unavailable or incomplete."""
    for endpoint in (DISCOVERY, DISCOVERY_FALLBACK):
        try:
            packet = source.fetch(endpoint, 'all')
            rows = list(packet['rows'])
            if endpoint == DISCOVERY_FALLBACK:
                converted = []
                for row in rows:
                    symbol = str(row.get('code', ''))
                    if not re.fullmatch(r'(?:sh|sz|bj)\d{6}', symbol):
                        raise VendorUnavailableError('腾讯全市场代码格式异常')
                    cap = number(row.get('zsz'))
                    # Tencent zsz is in CNY 100 millions; discovery expects CNY.
                    converted.append({'代码': symbol[2:], '名称': row.get('name'),
                                      '总市值': cap * 1e8 if cap is not None else None})
                rows = converted
            # Validate discovery before applying a requested subset: a missing
            # requested code may still be recovered from the exchange roster.
            _, coverage = discover(rows)
            if coverage['scanned'] < 4000:
                raise VendorUnavailableError('全A股行情少于4000只，疑似分页缺失')
            if roster is not None:
                seen = {str(row['代码']) for row in rows}
                missing = sorted(roster['names'].keys() - seen)
                # Unknown market cap must remain eligible for checking, never disappear.
                rows += [{'代码': code, '名称': roster['names'][code], '总市值': None}
                         for code in missing]
            universe, coverage = discover(rows, codes)
            if roster is not None:
                coverage['roster'] = {
                    'status': roster['status'], 'expected': len(roster['names']),
                    'observed': len(seen), 'missing_quote_codes': missing,
                    'unlisted_quote_codes': sorted(seen - roster['names'].keys()),
                    'boards': roster['boards'],
                }
            coverage['fallback_used'] = endpoint != DISCOVERY
            return universe, coverage, packet
        except VendorUnavailableError:
            continue
    raise VendorUnavailableError('东方财富及腾讯全市场发现均失败或不完整；旧快照未覆盖')


def price_risk(rows, asof):
    """Adjusted close daily risk, excluding today's potentially unfinished bar."""
    metrics = dict.fromkeys(('volatility_1y_pct', 'volatility_3y_pct',
                            'max_drawdown_1y_pct', 'max_drawdown_3y_pct', 'turnover_20d_yi'))
    notes, points, seen = [], [], set()
    for row in rows:
        day = calendar_date(row.get('日期'))
        if day and day >= asof:
            continue
        close = number(row.get('收盘'))
        if not day or close is None or close <= 0 or day in seen:
            return metrics, ['复权行情存在无效日期、价格或重复记录，低波指标未计算']
        seen.add(day)
        points.append((day, close, number(row.get('成交额'))))
    points.sort()
    if not points or (asof - points[-1][0]).days > 4:
        return metrics, ['复权行情缺失或距筛选日超过4个自然日，低波指标未计算']
    metrics['risk_as_of'] = points[-1][0].isoformat()
    metrics['price_adjustment'] = 'qfq'
    for years, minimum in ((1, 200), (3, 600)):
        start = asof.replace(year=asof.year - years, day=28) if asof.month == 2 and asof.day == 29 else asof.replace(year=asof.year - years)
        # Include one preceding observation to measure the first daily return.
        before = [p for p in points if p[0] <= start]
        window = ([before[-1]] if before else []) + [p for p in points if p[0] > start]
        if len(window) < minimum + 1 or abs((window[0][0] - start).days) > 14:
            notes.append(f'{years}年复权行情覆盖不足（至少{minimum}个日收益），指标待核验')
            continue
        returns = [b[1] / a[1] - 1 for a, b in zip(window, window[1:], strict=False)]
        peak, drawdown = window[0][1], 0
        for _, close, _ in window:
            peak = max(peak, close)
            drawdown = max(drawdown, 1 - close / peak)
        metrics[f'volatility_{years}y_pct'] = statistics.stdev(returns) * math.sqrt(252) * 100
        metrics[f'max_drawdown_{years}y_pct'] = drawdown * 100
    amounts = [p[2] for p in points[-20:]]
    if len(amounts) == 20 and all(v is not None and v >= 0 for v in amounts):
        metrics['turnover_20d_yi'] = statistics.mean(amounts) / 1e8
    else:
        notes.append('近20个交易日成交额缺失，流动性待核验')
    return metrics, notes


def dividend_metrics(quote, rows, asof):
    """Calculate annual and trailing yields from dated, implemented cash events."""
    metrics, checks = {}, []

    def check(label, condition, detail='', required=True):
        checks.append({'label': label, 'status': 'pending' if condition is None else 'pass' if condition else 'fail', 'detail': detail, 'required': required})

    price = number(quote.get('price'))
    try:
        quote_date = datetime.strptime(str(quote.get('quote_time') or '')[:8], '%Y%m%d').date()
        fresh = 0 <= (asof - quote_date).days <= 4
    except ValueError:
        fresh = False
    complete_dividends = bool(rows)
    review_start = date(asof.year - 5, 1, 1)
    implemented = []
    effective = []
    seen = {}
    excluded_history = []
    for row in rows:
        ex = calendar_date(row.get('除权除息日'))
        notice = calendar_date(row.get('最新公告日期'))
        report = calendar_date(row.get('报告期'))
        # Two independently dated fields establish that an event is outside
        # the review window. Any recent event/notice vetoes that exclusion.
        if (report and report < review_start and (ex or notice)
                and all(d < review_start for d in (ex, notice) if d)):
            excluded_history.append({'report': report.isoformat(),
                                     'ex_date': ex.isoformat() if ex else None,
                                     'notice': notice.isoformat() if notice else None})
            continue
        if not row.get('方案进度'):
            complete_dividends = False
            continue
        if row.get('方案进度') != '实施分配':
            continue
        if (ex and ex > asof) or (notice and notice > asof):
            continue
        cash = cash_ratio(row)
        split = split_ratio(row)
        if not (ex and notice and report and report <= notice <= ex and cash is not None
                and cash >= 0 and split is not None and split >= 0):
            complete_dividends = False
            continue
        key = (report, ex)
        values = (cash, split)
        if key in seen:
            if seen[key] != values:
                complete_dividends = False
            continue
        seen[key] = values
        row = dict(row)
        if cash_plan(row) is not None and '中小股东' in str(row.get('现金分红-现金分红比例描述')):
            row.setdefault('source_cash_per10', row.get('现金分红-现金分红比例'))
            row['cash_parse_basis'] = 'explicit_minority_shareholder_pretax_per10'
        elif number(row.get('现金分红-现金分红比例')) is None:
            row['cash_parse_basis'] = 'explicit_pure_bonus_or_transfer'
        row['现金分红-现金分红比例'] = cash
        effective.append(row)
        if cash > 0:
            implemented.append(row)
    share_events = {}
    for event in effective:
        ex = calendar_date(event['除权除息日'])
        split = split_ratio(event)
        if split > 0:
            if ex in share_events and share_events[ex] != split:
                complete_dividends = False
            share_events[ex] = split
    metrics['dividend_excluded_history'] = excluded_history
    check('分红数据字段完整', True if complete_dividends else None,
          (f'已按报告期及实施/公告日期排除{len(excluded_history)}条核验窗口之前的历史记录；原始记录保留在来源缓存'
           if excluded_history else '') if complete_dividends
          else '核验期间内或无法确定期间的已实施记录字段缺失/无效，不能按零值计算')
    # Calendar anniversary window (exclusive start), including leap-year handling.
    start = asof.replace(year=asof.year - 1, day=min(asof.day, 28)) if asof.month == 2 and asof.day == 29 else asof.replace(year=asof.year - 1)
    trailing = [r for r in implemented if str(r['除权除息日'])[:10] > start.isoformat()]
    def current_cash(event):
        # Cash and bonus in the same event both use pre-ex shares. Later bonuses
        # also expand that share basis; earlier bonuses must not dilute new cash.
        ex = calendar_date(event['除权除息日'])
        factor = math.prod(1 + ratio / 10 for day, ratio in share_events.items() if day >= ex)
        return number(event['现金分红-现金分红比例']) / 10 / factor

    dividend = sum(current_cash(r) for r in trailing)
    yield_pct = dividend / price * 100 if complete_dividends and price and price > 0 and fresh else None
    metrics.update(dividend_ttm=dividend if complete_dividends else None, yield_ttm_pct=yield_pct)
    year = asof.year - 1
    annual_rows = [r for r in implemented if str(r['报告期'])[:4] == str(year)]
    final_present = any(str(r['报告期'])[:10] == f'{year}-12-31' for r in effective)
    annual_cash = (sum(current_cash(r) for r in annual_rows)
                   if complete_dividends and final_present else None)
    annual_yield = annual_cash / price * 100 if annual_cash is not None and price and price > 0 and fresh else None
    metrics.update(dividend_annual=annual_cash, dividend_year=year, yield_pct=annual_yield,
                   yield_basis='annual_completed_v2', share_basis='implemented_bonus_adjusted')
    metrics['dividend_annual_history'] = [
        {'year': y, 'dps': sum(current_cash(r) for r in implemented
                                 if str(r['报告期'])[:4] == str(y))
         if complete_dividends and any(str(r['报告期'])[:10] == f'{y}-12-31'
                                      for r in effective) else None}
        for y in range(year, year - 3, -1)]
    # EPS is on a report-period share basis. Do not guess its conversion after
    # bonuses/transfers; even an ex-date before the cash payment can change EPS.
    metrics['payout_share_basis_comparable'] = not any(
        day >= date(year, 1, 1) for day in share_events)
    detail = (f'{year}年度已实施现金分红按后续及同期送转折算每股 / 行情价格；不含其他年度分红'
              if annual_cash is not None else f'{year}年度年末分红实施记录缺失或数据无效，待核验')
    check('最近完整年度税前股息率>4%', annual_yield > 4 if annual_yield is not None else None, detail)
    years = [str(asof.year - i) for i in range(1, 6)]
    paid_years = {str(r['报告期'])[:4] for r in implemented}
    check('连续5个完整年度分红', all(y in paid_years for y in years) if complete_dividends else None, '、'.join(years))
    return metrics, checks, effective


def core_profile(row, asof):
    """Independent, explicit research preset; missing evidence never passes."""
    from tradingagents.dataflows.dividend_official import listing_check

    checks = listing_check(row['metrics'], asof)
    m = row['metrics']

    def check(label, condition, detail=''):
        checks.append({'label': label, 'status': 'pending' if condition is None
                       else 'pass' if condition else 'fail', 'detail': detail, 'required': True})

    check('基础量化核验', None if row['status'] == 'pending' else row['status'] == 'pass',
          '基础列表必需项须全部通过；具体缺项见基础核验明细')
    history = m.get('dividend_annual_history', [])
    expected = list(range(asof.year - 1, asof.year - 4, -1))
    valid = (isinstance(history, list) and len(history) == 3
             and all(isinstance(r, dict) and r.get('year') == y
                     and number(r.get('dps')) is not None for r, y in zip(history, expected, strict=True)))
    amounts = [number(r['dps']) for r in history] if valid else []
    check('近3年每股分红每年降幅≤20%',
          all(d > 0 for d in amounts) and all(amounts[i] >= .8 * amounts[i+1]
                                             for i in range(2)) if valid else None,
          ('；'.join(f"{r['year']}年 {number(r['dps']):.4f}元/股" for r in history)
           if valid else '完整年度分红记录缺失')
          + '；按同一现有股份口径比较已实施分红')
    payout = number(m.get('payout_ratio_pct'))
    check('年度每股派息率>0且≤80%', 0 < payout <= 80 if payout is not None else None,
          (f'实际 {payout:.2f}%' if payout is not None else '实际值缺失')
          + '；每股年度分红/同年度基本EPS；不区分特别分红')
    risk_date = calendar_date(m.get('risk_as_of'))
    check('低波行情有效且不超过4个自然日',
          True if risk_date and 0 <= (asof - risk_date).days <= 4 else None,
          f'行情截至 {risk_date.isoformat() if risk_date else "缺失"}；筛选日 {asof.isoformat()}')
    for key, label, limit, minimum in (
            ('volatility_1y_pct', '1年年化波动率≤25%', 25, False),
            ('volatility_3y_pct', '3年年化波动率≤25%', 25, False),
            ('max_drawdown_1y_pct', '1年最大回撤≤25%', 25, False),
            ('max_drawdown_3y_pct', '3年最大回撤≤35%', 35, False),
            ('turnover_20d_yi', '20日日均成交额≥1亿元', 1, True)):
        value = number(m.get(key))
        check(label, (value >= limit if minimum else 0 <= value <= limit)
              if value is not None else None,
              (f'实际 {value:.2f}{"亿元" if minimum else "%"}'
               if value is not None else '实际值缺失，不能视为零风险')
              + f'；行情截至 {risk_date.isoformat() if risk_date else "缺失"}')
    statuses = [c['status'] for c in checks]
    return {'status': 'fail' if 'fail' in statuses else 'pending' if 'pending' in statuses
            else 'pass', 'checks': checks}


def _cached_financials(row, directory):
    """Read only the exact financial packets recorded in this snapshot; no requests."""
    datasets = {}
    for kind in ('profit', 'cashflow', 'balance'):
        recorded = row.get('provenance', {}).get(kind, {})
        endpoint, symbol = ENDPOINTS[kind], market_symbol(row['code'])
        key = hashlib.sha256(f'{endpoint}:{symbol}:'.encode()).hexdigest()[:20]
        path = Path(directory) / f'{key}.json'
        try:
            packet = json.loads(path.read_text(encoding='utf-8'))
            if (recorded.get('fetched_at') and packet.get('fetched_at') == recorded['fetched_at']
                    and packet.get('endpoint') == endpoint and packet.get('symbol') == symbol
                    and isinstance(packet.get('rows'), list)
                    and all(isinstance(r, dict) for r in packet['rows'])):
                datasets[kind] = packet['rows']
        except (OSError, ValueError, TypeError):
            continue
    return datasets


def annual_snapshot(data, cache_directory=None, official_path=None):
    """Project saved screening records onto the annual basis without changing the file."""
    import copy

    from tradingagents.dataflows.dividend_official import apply_evidence, load_evidence

    data = copy.deepcopy(data)
    records, evidence_error = load_evidence(official_path, date.fromisoformat(data['as_of']))
    data['official_evidence_error'] = evidence_error
    changed = False
    for row in data['items']:
        if cache_directory and row['metrics'].get('sustainability_basis') != 'annual_v1':
            datasets = _cached_financials(row, cache_directory)
            # Re-evaluate only when original dividend evidence is valid and all
            # financial packets used in the original record can be reproduced.
            needed = {k for k in ('profit', 'cashflow', 'balance')
                      if k in row.get('provenance', {})}
            if ('profit' in needed and needed <= datasets.keys()
                    and isinstance(row.get('dividend_events'), list)
                    and any(c['label'] == '分红数据字段完整' and c['status'] == 'pass'
                            for c in row['checks'])):
                datasets['dividends'] = row['dividend_events']
                evaluated = evaluate(row['code'], row['name'], row['sector'], row['metrics'],
                                     datasets, date.fromisoformat(data['as_of']))
                for key in ('metrics', 'checks', 'status'):
                    row[key] = evaluated[key]
        if row['metrics'].get('yield_basis') == 'annual_completed_v2':
            continue
        events = row.get('dividend_events', [])
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            events = []
        if any(c['label'] == '分红数据字段完整' and c['status'] != 'pass' for c in row['checks']):
            events = []
        metrics, checks, _ = dividend_metrics(row['metrics'], events,
                                             date.fromisoformat(data['as_of']))
        row['metrics'].update(metrics)
        row['checks'] = [c for c in row['checks'] if c['label'] not in
                         ('税前TTM股息率>4%', '最近完整年度税前股息率>4%', '分红数据字段完整', '连续5个完整年度分红')]
        row['checks'].extend(checks)
        required = [c['status'] for c in row['checks'] if c['required']]
        row['status'] = 'fail' if 'fail' in required else 'pending' if 'pending' in required else 'pass'
        changed = True
    if changed:
        data['yield_recalculated'] = True
        data['rules'] = data['rules'].replace('税前TTM股息率>4%', '最近完整年度税前股息率>4%')
    data['core_rules'] = CORE_RULES
    rechecked = sum(r['metrics'].get('sustainability_basis') == 'annual_v1' for r in data['items'])
    data['sustainability_rechecked'] = rechecked
    if rechecked:
        data['rules'] = RULES + (f' 本快照{rechecked}只已按本版财务规则核验；其余旧记录保留原规则，缺项需刷新。'
                                if rechecked < len(data['items']) else '')
    for row in data['items']:
        # Old snapshots can reconstruct dividend trends, but not missing EPS.
        if 'dividend_annual_history' not in row['metrics']:
            events = row.get('dividend_events', [])
            if (isinstance(events, list) and all(isinstance(e, dict) for e in events)
                    and any(c['label'] == '分红数据字段完整' and c['status'] == 'pass'
                            for c in row['checks'])):
                values, _, _ = dividend_metrics(row['metrics'], events,
                                                date.fromisoformat(data['as_of']))
                row['metrics']['dividend_annual_history'] = values['dividend_annual_history']
        apply_evidence(row, records, date.fromisoformat(data['as_of']))
        row['core'] = core_profile(row, date.fromisoformat(data['as_of']))
    return data


def evaluate(code, name, sector, quote, datasets, asof):
    checks = []
    metrics = dict(quote)
    special_finance = sector == '银行' or any(word in sector for word in ('保险', '证券', '多元金融'))

    def check(label, condition, detail='', required=True):
        checks.append({'label': label, 'status': 'pending' if condition is None else 'pass' if condition else 'fail', 'detail': detail, 'required': required})

    cap = number(quote.get('market_cap_yi'))
    try:
        quote_date = datetime.strptime(str(quote.get('quote_time') or '')[:8], '%Y%m%d').date()
        fresh = 0 <= (asof - quote_date).days <= 4
    except ValueError:
        fresh = False
    check('行情有效且不超过4个自然日', True if fresh and number(quote.get('price')) and number(quote.get('price')) > 0 else None)
    check('总市值≥500亿元', cap >= 500 if cap is not None and fresh else None)
    dividend_values, dividend_checks, implemented = dividend_metrics(quote, datasets.get('dividends', []), asof)
    metrics.update(dividend_values)
    checks.extend(dividend_checks)
    years = [str(asof.year - i) for i in range(1, 6)]
    cf = annual(datasets.get('cashflow', []), asof)
    profits = annual(datasets.get('profit', []), asof)
    balance = annual(datasets.get('balance', []), asof)
    for label, statements in [('利润表', profits), ('现金流量表', cf), ('资产负债表', balance)]:
        if special_finance and label != '利润表':
            continue
        valid_currency = all(statements.get(y, {}).get('CURRENCY') == 'CNY' for y in years[:3])
        check(f'{label}人民币口径', True if valid_currency else None)
        for year in list(statements):
            if statements[year].get('CURRENCY') != 'CNY':
                del statements[year]
    recent = years[:3]
    cfo = [number(cf.get(y, {}).get('NETCASH_OPERATE')) for y in recent]
    profit = [number(profits.get(y, {}).get('PARENT_NETPROFIT')) for y in recent]
    net = [number(profits.get(y, {}).get('NETPROFIT')) for y in recent]
    metrics.update(annual_years=recent, cfo_yuan=cfo, parent_profit_yuan=profit,
                   net_profit_yuan=net, sustainability_basis='annual_v1')
    eps = number(profits.get(years[0], {}).get('BASIC_EPS'))
    metrics['basic_eps_annual'] = eps
    # Dividend source EPS is rounded; materially inconsistent denominators must
    # not produce an apparently precise payout ratio.
    event_eps = [number(r.get('每股收益')) for r in implemented
                 if str(r.get('报告期'))[:10] == f'{years[0]}-12-31']
    consistent = all(e is None or (eps is not None and abs(e - eps) <= max(.01, abs(eps)*.01))
                     for e in event_eps)
    dps = number(metrics.get('dividend_annual'))
    metrics['payout_ratio_pct'] = (dps / eps * 100 if eps and eps > 0 and dps is not None
                                 and metrics['payout_share_basis_comparable'] and consistent else None)
    capex = [number(cf.get(y, {}).get('CONSTRUCT_LONG_ASSET')) for y in recent]
    metrics['fcf_proxy_yuan'] = [c - k if c is not None and k is not None else None for c, k in zip(cfo, capex, strict=True)]
    pe = cap * 1e8 / profit[0] if cap and profit[0] and profit[0] > 0 and fresh else None
    metrics['pe_annual'] = pe
    check('静态PE>0且≤20', 0 < pe <= 20 if pe is not None else None, f'市值/归母净利润（{years[0]}年报），非TTM PE')
    check('3年盈利且最近年降幅≤30%', all(p > 0 for p in profit) and profit[0] >= .7 * profit[1] if all(p is not None for p in profit) else None)
    latest = balance.get(years[0], {})
    assets, debt = number(latest.get('TOTAL_ASSETS')), number(latest.get('TOTAL_LIABILITIES'))
    ratio = debt / assets if assets and assets > 0 and debt is not None else None
    metrics['debt_ratio_pct'] = ratio * 100 if ratio is not None else None
    if special_finance:
        values, sector_checks = financial_checks(datasets.get('indicators', []), sector, asof)
        metrics.update(values)
        checks.extend(sector_checks)
    else:
        cash_ok = all(v > 0 for v in cfo) if all(v is not None for v in cfo) else None
        check('近3年经营现金流均为正', cash_ok)
        check('3年累计经营现金流覆盖累计净利润',
              all(n > 0 for n in net) and sum(cfo) >= sum(net)
              if all(v is not None for v in cfo + net) else None,
              '合并报表同期间累计比较；不等同于现金流覆盖归母分红；不设单年降幅门槛')
        check('年报资产负债率≤75%', ratio <= .75 if ratio is not None else None)
    if sector == '待识别':
        check('行业适用性复核', None, '行业缺失或属于特殊金融行业，普通企业财务门槛不能单独证明安全')
    required_checks = [c for c in checks if c['required']]
    status = 'fail' if any(c['status'] == 'fail' for c in required_checks) else 'pending' if any(c['status'] == 'pending' for c in required_checks) else 'pass'
    return {'code': code, 'name': name, 'sector': sector, 'status': status, 'metrics': metrics,
            'checks': checks, 'dividend_events': implemented,
            'sources': {'dividends': f'https://data.eastmoney.com/yjfp/detail/{code}.html',
                        'financials': f'https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index?type=web&code={market_symbol(code)}',
                        'quotes': 'https://qt.gtimg.cn/'}}


def run(directory, direct=False, codes=None, cache_directory=None,
        official_path=None):
    directory = Path(directory)
    asof = date.today()
    source = Source(cache_directory or directory / 'dividend-cache', direct=direct)
    roster = exchange_roster(source)
    universe, coverage, discovery = discover_market(source, codes, roster=roster)
    print(f'UNIVERSE {len(universe)}', flush=True)
    quotes = source.quotes([r[0] for r in universe])

    def one(item):
        code, name, sector = item
        data, provenance, errors = {}, {}, []
        data['dividends'], dividend_source, dividend_errors = fetch_dividends(source, code)
        if dividend_source:
            provenance['dividends'] = dividend_source
        errors.extend(dividend_errors)
        values, _, _ = dividend_metrics(quotes.get(code, {}), data.get('dividends', []), asof)
        if values['yield_pct'] is not None and values['yield_pct'] <= 4:
            result = evaluate(code, name, sector, quotes.get(code, {}), data, asof)
            if dividend_source.get('endpoint') == DIVIDENDS_FALLBACK:
                result['sources']['dividends'] = f'https://webapi.cninfo.com.cn/#/company?companyid={code}'
            for check in result['checks']:
                if check['status'] == 'pending':
                    check['detail'] = '年度股息率未超过4%，本轮未继续采集财务与低波数据'
            result.update(errors=errors, provenance=provenance,
                          collection_stage='yield_excluded',
                          risk_notes=['年度股息率未超过4%，未采集低波数据'])
            print(f'{code} {name}: fail', flush=True)
            return result
        try:
            packet = source.fetch(PROFILE, code)
            sector = next((str(r['value']).strip() for r in packet['rows']
                           if r.get('item') == '行业' and r.get('value')), '待识别')
            if '银行' in sector:
                sector = '银行'
            provenance['industry'] = {k: v for k, v in packet.items() if k != 'rows'}
        except VendorUnavailableError as exc:
            errors.append(str(exc))
        for kind, endpoint in ENDPOINTS.items():
            if kind == 'dividends':
                continue
            if sector == '银行' and kind in ('cashflow', 'balance'):
                continue
            try:
                packet = source.fetch(endpoint, code if kind == 'dividends' else market_symbol(code))
                data[kind] = packet['rows']
                provenance[kind] = {k: v for k, v in packet.items() if k != 'rows'}
            except VendorUnavailableError as exc:
                errors.append(str(exc))
        if financial_sector(sector):
            try:
                packet = source.fetch(INDICATORS, market_symbol(code))
                data['indicators'] = packet['rows']
                provenance['indicators'] = {k: v for k, v in packet.items() if k != 'rows'}
            except VendorUnavailableError as exc:
                errors.append(str(exc))
        result = evaluate(code, name, sector, quotes.get(code, {}), data, asof)
        if dividend_source.get('endpoint') == DIVIDENDS_FALLBACK:
            result['sources']['dividends'] = f'https://webapi.cninfo.com.cn/#/company?companyid={code}'
        try:
            packet = source.fetch(HISTORY, code)
            risk, risk_notes = price_risk(packet['rows'], asof)
            provenance['prices'] = {k: v for k, v in packet.items() if k != 'rows'}
        except VendorUnavailableError as exc:
            risk, risk_notes = price_risk([], asof)
            errors.append(str(exc))
        result['metrics'].update(risk)
        result['sources']['prices'] = f'https://quote.eastmoney.com/{market_symbol(code).lower()}.html'
        result.update(errors=errors, risk_notes=risk_notes, provenance=provenance,
                      collection_stage='full')
        print(f'{code} {name}: {result["status"]}', flush=True)
        return result

    with ThreadPoolExecutor(max_workers=3) as executor:
        items = list(executor.map(one, universe))
    items.sort(key=lambda r: -(r['metrics']['yield_pct'] or -1))
    coverage.update(evaluated=len(items),
                    yield_excluded=sum(r['collection_stage'] == 'yield_excluded' for r in items),
                    risk_complete=sum(all(r['metrics'].get(k) is not None for k in
                        ('volatility_1y_pct', 'volatility_3y_pct', 'max_drawdown_1y_pct',
                         'max_drawdown_3y_pct', 'turnover_20d_yi')) for r in items),
                    provider_error_count=sum(bool(r['errors']) for r in items),
                    source={k: v for k, v in discovery.items() if k != 'rows'})
    payload = {'schema_version': 2, 'require_qualifications': False,
               'generated_at': datetime.now().astimezone().isoformat(),
               'as_of': asof.isoformat(), 'rules': RULES, 'discovery': coverage,
               'coverage': f"全A股行情 {coverage['scanned']} 只，市值不足500亿元排除 "
                           f"{coverage['excluded_small_cap']} 只，逐项核验 {len(items)} 只"
                           + ('（指定代码子集）' if codes else ''),
               'limitations': '通过仅表示满足本版量化规则，不能保证不爆雷。未完整检查审计意见、质押、特别分红、未来分红和最新季度恶化。银行及证券按行业研究阈值核验，非完整监管合规评估；保险核心/综合偿付能力主体口径、评级及投资资产质量仍需公告核验。A/H股总市值采用行情源口径。低波指标来自前复权价格，不是含税后分红再投资的总收益；不代表未来波动。交易所名单对账状态及行情遗漏代码见 discovery.roster；接口失败时覆盖范围未确认。',
               'items': items, 'trading_calendar': trading_calendar(source)}
    from tradingagents.dataflows.dividend_official import FILENAME

    payload = annual_snapshot(payload, official_path=official_path or directory / FILENAME)
    print('正在评估大盘与候选行业价格环境…', flush=True)
    payload['market_context'] = collect_market_context(source, payload['items'], asof)
    atomic_json(directory / 'dividend-screen.json', payload)
    write_summary(directory, payload)
    return payload


def write_summary(directory, payload):
    lines = ['# 高股息筛选快照', '', payload['generated_at'], '', payload['coverage'], '', payload['rules'], '',
             payload.get('core_rules', CORE_RULES), '',
             '|股票|最近完整年度税前股息率|静态PE|基础核验|红利低波精选|未通过/待核验|', '|---|---:|---:|---|---|---|']
    labels = {'pass': '量化通过', 'pending': '待核验', 'fail': '未通过'}
    for row in payload['items']:
        m = row['metrics']
        y = f'{m["yield_pct"]:.2f}%' if m.get('yield_pct') is not None else '缺失'
        pe = f'{m["pe_annual"]:.2f}' if m.get('pe_annual') is not None else '缺失'
        reasons = '；'.join(c['label'] + ('（待核验）' if c['status'] == 'pending' else '') for c in row['checks'] if c['required'] and c['status'] != 'pass')
        core = row.get('core') or core_profile(row, date.fromisoformat(payload['as_of']))
        reasons += '；精选：' + '；'.join(c['label'] for c in core['checks'] if c['status'] != 'pass')
        lines.append(f'|{row["code"]} {row["name"]}|{y}|{pe}|{labels[row["status"]]}|{labels[core["status"]]}|{reasons}|')
    lines += ['', payload['limitations'], '', '详细指标、分红事件、数据来源和抓取时间见同目录 dividend-screen.json。']
    temporary = directory / 'dividend-screen.md.tmp'
    temporary.write_text('\n'.join(lines), encoding='utf-8')
    temporary.replace(directory / 'dividend-screen.md')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('data/desktop'))
    parser.add_argument('--direct', action='store_true', help='仅本次抓取直连，不修改系统代理')
    parser.add_argument('--cache-dir', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--environment-snapshot', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--official-evidence', type=Path, help='已核实公告证据 JSON 路径')
    parser.add_argument('--codes', nargs='+', help='全A股发现后仅核验指定代码（仍须满足市值门槛）')
    parser.add_argument('--worker', nargs=2, choices=None, help=argparse.SUPPRESS)
    parser.add_argument('--check-runtime', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check_runtime:
        import akshare

        for endpoint in (*ENDPOINTS.values(), DISCOVERY, DISCOVERY_FALLBACK, HISTORY, PROFILE, INDICATORS, DIVIDENDS_FALLBACK, INDEX_HISTORY, INDEX_FALLBACK, SECTOR_HISTORY, *ROSTER_ENDPOINTS):
            if not callable(getattr(akshare, endpoint, None)):
                raise RuntimeError(f'AKShare endpoint unavailable: {endpoint}')
        print('dividend-runtime-ready', flush=True)
    elif args.worker:
        endpoint, symbol = args.worker
        allowed = (*ENDPOINTS.values(), DISCOVERY, DISCOVERY_FALLBACK, HISTORY, PROFILE,
                   INDICATORS, DIVIDENDS_FALLBACK, TRADE_CALENDAR, INDEX_HISTORY, INDEX_FALLBACK,
                   SECTOR_HISTORY, *ROSTER_ENDPOINTS)
        symbol_valid = (bool(re.fullmatch(r'[\w\u4e00-\u9fff-]{1,40}', symbol))
                        if endpoint == SECTOR_HISTORY else
                        bool(re.fullmatch(r'(?:(?:SH|SZ|BJ|sh)?\d{6}|all|star)', symbol)))
        if endpoint not in allowed or not symbol_valid:
            parser.error('invalid worker request')
        worker(endpoint, symbol)
    elif args.environment_snapshot:
        payload = json.loads(args.environment_snapshot.read_text(encoding='utf-8'))
        asof = date.fromisoformat(payload['as_of'])
        if not 0 <= (date.today() - asof).days <= 4:
            parser.error('snapshot is stale; refresh the full screen')
        payload['market_context'] = collect_market_context(
            Source(args.cache_dir or args.data_dir / 'dividend-cache', direct=args.direct),
            payload['items'], asof)
        atomic_json(args.data_dir / 'dividend-screen.json', payload)
    else:
        run(args.data_dir, args.direct, args.codes, cache_directory=args.cache_dir,
            official_path=args.official_evidence)


if __name__ == '__main__':
    main()
