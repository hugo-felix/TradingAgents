"""Disclosed gross A-share dividend schemes and their publication boundaries."""

from decimal import Decimal

import pandas as pd

from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import VendorUnavailableError
from tradingagents.dataflows.facts import Fact, SourceResult

from . import common


def _dividend_cash(value, per_share: bool = False) -> str:
    """Preserve source decimal precision when converting per-ten-share cash."""
    from decimal import Decimal, InvalidOperation

    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return 'unavailable'
        return format(amount / 10 if per_share else amount, 'f')
    except InvalidOperation:
        return 'unavailable'


def get_dividends_result(ticker: str, as_of_date: str | None = None) -> SourceResult:
    """Disclosed A-share cash schemes; ancillary valuation fields are excluded."""
    from datetime import UTC, datetime

    code, _ = common.resolve_symbol(ticker)
    as_of_date = as_of_date or date_window.get_current_date()
    cutoff = pd.Timestamp(as_of_date).normalize()
    endpoint = 'stock_fhps_detail_em'
    header = (f'# Dividend schemes for {ticker} (AKShare)\n'
              f'# Source: AKShare / Eastmoney ({endpoint}); as of {as_of_date}; '
              f'fetched at {datetime.now(UTC).isoformat()}\n')
    if date_window.is_historical(as_of_date):
        return SourceResult(header + 'Dividend schemes are withheld: current scheme revisions have no complete '
                'historical revision archive; original amounts and stages cannot be verified.\n')
    data = common.request(endpoint, symbol=code)
    gap = ('Dividend coverage is insufficient for disclosed schemes through this date; '
           'this does not establish that the company paid no dividends.\n')
    if data.empty:
        return SourceResult(header + gap)
    required = {'报告期', '最新公告日期', '现金分红-现金分红比例', '方案进度'}
    if not required.issubset(data.columns):
        raise VendorUnavailableError(f'AKShare {endpoint} is missing required dividend columns')
    data = data.copy()
    for col in ('报告期', '最新公告日期', '预案公告日', '股权登记日', '除权除息日'):
        data[col] = pd.to_datetime(data[col], errors='coerce') if col in data else pd.NaT
    data = data[(data['最新公告日期'] <= cutoff) & (data['报告期'] <= cutoff)]
    if data.empty:
        return SourceResult(header + gap)
    data = data.sort_values(['报告期', '最新公告日期'], ascending=False).head(12)
    result = data[['报告期', '预案公告日', '最新公告日期', '股权登记日', '除权除息日', '方案进度']].copy()
    for col in result.columns[:-1]:
        result[col] = result[col].dt.strftime('%Y-%m-%d')
    result['Gross cash (CNY per 10 shares)'] = data['现金分红-现金分红比例'].map(_dividend_cash)
    result['Gross cash (CNY/share)'] = data['现金分红-现金分红比例'].map(
        lambda value: _dividend_cash(value, per_share=True))
    facts = []
    for index, (_, row) in enumerate(data.iterrows()):
        value = _dividend_cash(row['现金分红-现金分红比例'], per_share=True)
        period = row['报告期'].strftime('%Y-%m-%d')
        facts.append(Fact(key=f'akshare:dividend:{code}:{period}:{index}', label='Gross cash dividend',
                          value=None if value == 'unavailable' else Decimal(value), unit='CNY/share',
                          currency='CNY', period=period, basis='gross A-share cash per scheme',
                          status='missing' if value == 'unavailable' else 'available',
                          qualifiers=(f"Scheme stage: {row['方案进度']}",
                                      f"Latest announcement: {row['最新公告日期']:%Y-%m-%d}",
                                      f"Ex-dividend date: {row['除权除息日']}",
                                      'Gross/pre-tax; proposal is not proof of payment',
                                      'Converted A-share cash is not constant-currency declared dividend growth; does not establish policy stagnation'),
                          source_url=f'https://data.eastmoney.com/yjfp/detail/{code}.html'))
    return SourceResult(header + '# Current schemes, at most 12 rows; amounts are gross/pre-tax, not dividend yield.\n'
            '# Scheme stage is reported separately; a proposal is not proof of payment.\n'
            '# Registration/ex-dividend dates may be scheduled future dates; latest announcement must be on/before as-of.\n\n'
            + result.to_csv(index=False, na_rep='unavailable'), tuple(facts))


def get_dividends(ticker: str, as_of_date: str | None = None) -> str:
    return get_dividends_result(ticker, as_of_date).content
