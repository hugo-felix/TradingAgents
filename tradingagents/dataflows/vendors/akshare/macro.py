"""Explicit China macro indicators from AKShare, with conservative vintage guards."""
import re
from datetime import UTC, datetime

import pandas as pd

from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import VendorUnavailableError

from . import common

# Units checked against AKShare's official output schema on 2026-11-05:
# https://akshare.akfamily.xyz/data/macro/macro_china.html
_SERIES = {
    'china_cpi': ('macro_china_cpi', '月份', {
        '全国-同比增长': 'CPI YoY (%)', '全国-环比增长': 'CPI MoM (%)'},
        'National CPI percentage changes.'),
    'china_pmi': ('macro_china_pmi', '月份', {
        '制造业-指数': 'Manufacturing PMI (index)',
        '非制造业-指数': 'Non-manufacturing PMI (index)'},
        'PMI index levels; provider PMI growth percentages are excluded.'),
    'china_gdp': ('macro_china_gdp', '季度', {
        '国内生产总值-绝对值': 'GDP cumulative (CNY 100 million)',
        '国内生产总值-同比增长': 'GDP cumulative YoY (%)'},
        'GDP periods are year-to-date cumulative; preserve the original period label.'),
    'china_lpr': ('macro_china_lpr', 'TRADE_DATE', {
        'LPR1Y': 'LPR 1Y (%)', 'LPR5Y': 'LPR 5Y (%)'},
        'Dates are source TRADE_DATE; other historical lending-rate series are excluded.'),
    'china_credit': ('macro_china_new_financial_credit', '月份', {
        '当月': 'Credit monthly (CNY 100 million)', '累计': 'Credit YTD (CNY 100 million)'},
        'New financial credit monthly flow and year-to-date cumulative amount; not total social financing.'),
}


def _period_end(value) -> pd.Timestamp:
    """Parse Chinese months and cumulative quarter labels without losing their basis."""
    text = str(value)
    quarter = re.fullmatch(r'(\d{4})年第?([一二三四1-4])(?:-([一二三四1-4]))?季度', text)
    month = re.fullmatch(r'(\d{4})年(\d{1,2})月份?', text)
    try:
        if quarter:
            digit = quarter[3] or quarter[2]
            number = {'一': 1, '二': 2, '三': 3, '四': 4}.get(digit) or int(digit)
            return pd.Timestamp(int(quarter[1]), number * 3, 1) + pd.offsets.MonthEnd()
        if month:
            return pd.Timestamp(int(month[1]), int(month[2]), 1) + pd.offsets.MonthEnd()
        return pd.to_datetime(value, errors='coerce')
    except (ValueError, TypeError, OverflowError):
        return pd.NaT


def get_macro_data(indicator: str, as_of_date: str, look_back_days: int | None = None) -> str:
    """Return only an explicitly supported domestic series, never infer geography."""
    if indicator not in _SERIES:
        raise ValueError(f'AKShare does not support macro indicator {indicator!r}; use an explicit china_* identifier')
    if look_back_days is not None and (type(look_back_days) is not int or look_back_days <= 0):
        raise ValueError('look_back_days must be a positive integer or None')
    look_back_days = 365 if look_back_days is None else look_back_days
    cutoff = pd.Timestamp(as_of_date).normalize()
    if pd.isna(cutoff):
        raise ValueError('as_of_date must be a valid date')
    endpoint, period, fields, note = _SERIES[indicator]
    header = (f'# China macro: {indicator}\n'
              f'# Source: AKShare / Eastmoney ({endpoint}); as of {as_of_date}; '
              f'fetched at {datetime.now(UTC).isoformat()}\n')
    if date_window.is_historical(as_of_date):
        return (header + 'Macro values are withheld: AKShare does not provide a complete historical '
                'release vintage archive. Period dates cannot prove what was known at this date.\n')
    data = common.request(endpoint)
    gap = '# Macro coverage insufficient: no usable rows in the requested period window.\n'
    if data.empty:
        return header + gap
    if not {period, *fields}.issubset(data.columns):
        raise VendorUnavailableError(f'AKShare {endpoint} is missing required macro columns')
    data = data.copy()
    data['_period_end'] = data[period].map(_period_end)
    eligible = data['_period_end'].notna() & (data['_period_end'] <= cutoff)
    if look_back_days is not None:
        eligible &= data['_period_end'] >= cutoff - pd.Timedelta(days=look_back_days)
    for field in fields:
        data[field] = pd.to_numeric(data[field], errors='coerce').replace([float('inf'), float('-inf')], float('nan'))
    data = data.loc[eligible].dropna(subset=list(fields), how='all')
    data = data.sort_values('_period_end', ascending=False).head(24)
    if data.empty:
        return header + gap
    result = data[[period, '_period_end', *fields]].rename(
        columns={period: 'Source period', '_period_end': 'Period end / source date', **fields})
    result['Period end / source date'] = result['Period end / source date'].dt.strftime('%Y-%m-%d')
    return (header + f'# {note}\n'
            '# Period/source dates are not verified release dates. Current reported values only; '
            'this is not a complete historical release vintage database.\n'
            '# At most 24 rows; missing values are unavailable.\n'
            '# Unit schema: https://akshare.akfamily.xyz/data/macro/macro_china.html\n\n'
            + result.to_csv(index=False, na_rep='unavailable'))
