"""Current-vintage Eastmoney A-share history and explicit operator peers."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd

from tradingagents.dataflows import date_window, market_session
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError
from tradingagents.dataflows.facts import Fact, SourceResult
from tradingagents.dataflows.symbols import normalize_symbol

from . import common

_OPERATOR_PEERS = {'600941.SS': '中国移动', '601728.SS': '中国电信', '600050.SS': '中国联通'}
_METRICS = {'PE(TTM)': 'PE(TTM)', 'PB': '市净率'}
_EM_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_VALUEANALYSIS_DET'


def _positive(values):
    numeric = pd.to_numeric(values, errors='coerce')
    return numeric.where((numeric > 0) & (numeric < float('inf')))


def _history(data, ticker, as_of_date, years):
    """Source dates, rather than filled calendar dates, define observations."""
    if data.empty:
        return pd.DataFrame(columns=['Date', *_METRICS])
    if '数据日期' not in data:
        raise VendorUnavailableError('AKShare stock_value_em is missing 数据日期')
    data = data.rename(columns={'数据日期': 'Date'}).copy()
    data['Date'] = pd.to_datetime(data['Date'], errors='coerce').dt.normalize()
    data = data.dropna(subset=['Date']).drop_duplicates('Date', keep='last').sort_values('Date')
    data = market_session.completed_daily_bars(data, ticker, as_of_date)
    start = pd.Timestamp(as_of_date) - pd.DateOffset(years=years)
    data = data[data['Date'] >= start].copy()
    for label, source in _METRICS.items():
        data[label] = _positive(data[source]) if source in data else float('nan')
    return data[['Date', *_METRICS]]


def _value(value):
    return 'missing' if pd.isna(value) else f'{value:.10g}'


def _provenance(ticker, endpoint, url, fetched_at, data_date):
    return (f'# requested_stock={ticker}; endpoint={endpoint}; source_url={url}; '
            f'fetched_at={fetched_at}; data_date={data_date}\n')


def _cross_check(code, ticker, anchor, current, years, fetched_at):
    output = ['# Optional Baidu same-day cross-check; calendar-day series is never used for percentiles or to replace Eastmoney.\n',
              'metric,eastmoney,baidu,baidu_minus_eastmoney,status\n']
    for metric, indicator in [('PE(TTM)', '市盈率(TTM)'), ('PB', '市净率')]:
        try:
            data = common.request('stock_zh_valuation_baidu', symbol=code,
                                  indicator=indicator, period={1: '近一年', 3: '近三年', 5: '近五年'}[years])
            value = float('nan')
            if not data.empty:
                if not {'date', 'value'}.issubset(data.columns):
                    raise VendorUnavailableError('AKShare stock_zh_valuation_baidu is missing date/value')
                data = data.copy()
                data['date'] = pd.to_datetime(data['date'], errors='coerce').dt.normalize()
                same_day = data[data['date'] == anchor].drop_duplicates('date', keep='last')
                if not same_day.empty:
                    value = _positive(same_day['value']).iloc[0]
            em_value = current[metric]
            difference = value - em_value if pd.notna(value) and pd.notna(em_value) else float('nan')
            status = 'same-day comparison' if pd.notna(value) else 'missing: no valid same-day value'
            output.append(f'{metric},{_value(em_value)},{_value(value)},{_value(difference)},{status}\n')
            output.append(_provenance(ticker, 'stock_zh_valuation_baidu',
                                     f'https://gushitong.baidu.com/stock/ab-{code}', fetched_at,
                                     f'{anchor:%Y-%m-%d}' if pd.notna(value) else 'missing'))
        except (VendorUnavailableError, VendorNotConfiguredError) as exc:
            output.append(f'{metric},{_value(current[metric])},missing,missing,missing: {str(exc)[:600]}\n')
            output.append(_provenance(ticker, 'stock_zh_valuation_baidu',
                                     f'https://gushitong.baidu.com/stock/ab-{code}', fetched_at, 'missing'))
    return ''.join(output)


def get_valuation_comparison_result(ticker: str, as_of_date: str | None = None, look_back_years: int = 3) -> SourceResult:
    """Compare positive PE TTM/PB history and configured same-day A-share peers.

    These are provider-revised historical series, without an as-filed vintage
    guarantee. Accordingly, historical analysis dates are withheld entirely.
    """
    code, _ = common.resolve_symbol(ticker)
    canonical = normalize_symbol(ticker)
    as_of_date = date_window.get_current_date() if as_of_date is None else as_of_date
    try:
        parsed = date.fromisoformat(as_of_date)
    except (TypeError, ValueError) as exc:
        raise ValueError('as_of_date must be a valid YYYY-MM-DD date') from exc
    if parsed.isoformat() != as_of_date or as_of_date > date_window.get_current_date():
        raise ValueError('as_of_date must be YYYY-MM-DD and cannot be in the future')
    if type(look_back_years) is not int or look_back_years not in (1, 3, 5):
        raise ValueError('look_back_years must be 1, 3, or 5')
    fetched_at = datetime.now(UTC).isoformat()
    output = [f'# A-share valuation comparison for {ticker}; as_of_date={as_of_date}; look_back_years={look_back_years}\n',
              '# Source: AKShare / Eastmoney stock_value_em. PE(TTM) is trailing, not static PE; PB is price/book.\n',
              '# Current-vintage revised provider history has no as-filed/vintage guarantee. Percentiles are not fair value.\n']
    if date_window.is_historical(as_of_date):
        return SourceResult(''.join(output) + '# Historical valuation is withheld: historical revision vintages cannot be verified.\n')
    histories = {}
    failures = {}
    symbols = [canonical] + ([peer for peer in _OPERATOR_PEERS if peer != canonical]
                             if canonical in _OPERATOR_PEERS else [])
    for symbol in symbols:
        try:
            raw = common.request('stock_value_em', symbol=symbol.split('.')[0])
            histories[symbol] = _history(raw, symbol, as_of_date, look_back_years)
        except VendorNotConfiguredError:
            if symbol == canonical:
                raise
            failures[symbol] = 'AKShare SDK unavailable'
        except VendorUnavailableError as exc:
            failures[symbol] = str(exc)[:600]
    own = histories.get(canonical, pd.DataFrame())
    if own.empty:
        output.append('# self_status=unavailable; anchor_date=missing; no eligible self history.\n')
        for symbol in symbols:
            output.append(_provenance(symbol, 'stock_value_em', _EM_URL, fetched_at, 'missing' if symbol not in histories or histories[symbol].empty
                                      else f'{histories[symbol].iloc[-1]["Date"]:%Y-%m-%d}'))
            output.append(f'# {symbol}: {failures.get(symbol, "self anchor missing; same-day comparison unavailable")}\n')
        return SourceResult('DATA_UNAVAILABLE: no eligible current self valuation.\n' + ''.join(output))
    anchor = own.iloc[-1]['Date']
    current = own.iloc[-1]
    self_available = current[list(_METRICS)].notna().any()
    output.append(f'# self_status={"available" if self_available else "unavailable"}; anchor_date={anchor:%Y-%m-%d}\n')
    output.append('# Method: deduplicate source dates, sort, restrict to requested trailing calendar-year window and completed A-share sessions (15:15 Shanghai cutoff).\n'
                  '# Each metric uses only positive finite observations; percentile_le_pct = 100 * count(sample <= current) / valid_count, including current. No current-value backfill.\n'
                  '# Percentiles below are self history only; actual sample ranges may be shorter than the requested window.\n'
                  'metric,current,valid_count,actual_start,actual_end,percentile_le_pct\n')
    facts = []

    def measurement(symbol, metric, value, basis, suffix, unit='multiple', coverage=()):
        missing = value is None or pd.isna(value)
        facts.append(Fact(key=f'akshare:valuation:{symbol}:{anchor:%Y-%m-%d}:{metric}:{suffix}',
                          label=metric, value=None if missing else Decimal(str(value)), unit=unit,
                          period=f'{anchor:%Y-%m-%d}', basis=basis, status='missing' if missing else 'available',
                          qualifiers=('Current-vintage revised history; not fair value',
                                      'No backfill; only positive finite source observations', *coverage), source_url=_EM_URL))

    for metric in _METRICS:
        valid = own[own[metric].notna()]
        percentile = (valid[metric].le(current[metric]).mean() * 100
                      if not valid.empty and pd.notna(current[metric]) else float('nan'))
        start = f'{valid.iloc[0]["Date"]:%Y-%m-%d}' if not valid.empty else 'missing'
        end = f'{valid.iloc[-1]["Date"]:%Y-%m-%d}' if not valid.empty else 'missing'
        coverage = (f'Actual date coverage: {start} to {end}; requested trailing years: {look_back_years}',)
        measurement(canonical, metric, current[metric], 'current self valuation', 'current', coverage=coverage)
        measurement(canonical, metric, len(valid), 'positive finite self history sample count', 'count', 'observations', coverage)
        measurement(canonical, metric, percentile, 'self history percentile: 100 * count(sample <= current) / valid_count including current', 'percentile', '%', coverage)
        output.append(f'{metric},{_value(current[metric])},{len(valid)},{start},{end},{_value(percentile)}\n')
    output.append('# Fixed mainland operator peer set: 中国移动/中国电信/中国联通; no H-share substitutes.\n'
                  if canonical in _OPERATOR_PEERS else '# Peer coverage: no configured peers for this A-share.\n')
    output.append('# Same-day rows; anchor is self latest eligible source date and never rolls back for peers.\n'
                  'ticker,name,data_date,PE(TTM),PB,status\n')
    for symbol in symbols:
        history = histories.get(symbol, pd.DataFrame())
        same_day = history[history['Date'] == anchor] if not history.empty else history
        name = _OPERATOR_PEERS.get(symbol, 'self')
        if same_day.empty:
            output.append(f'{symbol},{name},{anchor:%Y-%m-%d},missing,missing,missing: {failures.get(symbol, "no same-day row")}\n')
        else:
            row = same_day.iloc[-1]
            status = 'available' if row[list(_METRICS)].notna().all() else 'partial/missing metric'
            output.append(f'{symbol},{name},{anchor:%Y-%m-%d},{_value(row["PE(TTM)"])},{_value(row["PB"])},{status}\n')
        for metric in _METRICS:
            measurement(symbol, metric, None if same_day.empty else same_day.iloc[-1][metric],
                        'same-day self valuation' if symbol == canonical else 'same-day peer valuation', 'same_day')
        latest = f'{history.iloc[-1]["Date"]:%Y-%m-%d}' if not history.empty else 'missing'
        output.append(_provenance(symbol, 'stock_value_em', _EM_URL, fetched_at, latest))
    output.append('# Latest self history sample (at most 8 source dates); full history used for counts above.\n')
    sample = own.tail(8).copy()
    sample['Date'] = sample['Date'].dt.strftime('%Y-%m-%d')
    for metric in _METRICS:
        sample[metric] = sample[metric].map(_value)
    output.append(sample.to_csv(index=False))
    if get_config().get('a_share_valuation_cross_check', False):
        output.append(_cross_check(code, canonical, anchor, current, look_back_years, fetched_at))
    prefix = '' if self_available else 'DATA_UNAVAILABLE: no valid current self PE(TTM) or PB.\n'
    return SourceResult(prefix + ''.join(output), tuple(facts))


def get_valuation_comparison(ticker: str, as_of_date: str | None = None, look_back_years: int = 3) -> str:
    return get_valuation_comparison_result(ticker, as_of_date, look_back_years).content
