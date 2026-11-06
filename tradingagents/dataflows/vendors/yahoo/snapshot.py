"""Deterministic market-data verification snapshot.

The market analyst is an LLM that can confabulate exact numbers — citing a
Bollinger band or a "historically validated bounce" that the underlying data
doesn't support (#830). This module computes a ground-truth snapshot (latest
OHLCV row on or before the analysis date, common indicators, recent closes)
the analyst is told to treat as the source of truth for any exact numeric
claim. Deterministic, no LLM involved.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.facts import Fact, SourceResult
from tradingagents.dataflows.market_session import completed_daily_bars, daily_bar_note
from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import price_text
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

# A fixed, common indicator set so the snapshot is the same shape every run.
DEFAULT_SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema", "close_50_sma", "close_200_sma",
    "rsi", "boll", "boll_ub", "boll_lb",
    "macd", "macds", "macdh", "atr",
)


def _verified_rows(symbol: str, as_of_date: str) -> pd.DataFrame:
    """OHLCV on or before as_of_date, date-sorted. Raises NoMarketDataError if nothing usable.

    ``load_ohlcv`` already normalizes the Date column and filters out
    look-ahead rows, but we re-apply the cutoff defensively — this is a
    verification path, so it must not trust its input to be pre-filtered.
    """
    # As reported: this snapshot is quoted by the agents as exact prices, so a
    # gap-filled cell would put the previous session's number under this date.
    data = load_ohlcv(symbol, as_of_date, fill_gaps=False)
    if data is None or data.empty:
        raise NoMarketDataError(symbol, normalize_symbol(symbol), "no price rows")

    df = completed_daily_bars(data, symbol, as_of_date).copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= pd.to_datetime(as_of_date)].sort_values("Date")
    if df.empty:
        raise NoMarketDataError(symbol, normalize_symbol(symbol), f"no price rows on or before {as_of_date}")
    return df


def _fmt(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return price_text(value)
    return str(value)


def build_verified_market_snapshot_result(
    symbol: str,
    as_of_date: str,
    look_back_days: int = 30,
    indicators: Iterable[str] | None = None,
) -> SourceResult:
    """Render a ground-truth snapshot: latest OHLCV row, indicators, recent closes."""
    # `df` keeps the original capitalized OHLCV columns (Open/High/Low/Close/
    # Volume); stockstats `wrap()` lowercases columns and adds indicator
    # columns, so read raw prices from `df` and indicators from `stock_df`.
    df = _verified_rows(symbol, as_of_date)
    stock_df = wrap(df.copy())

    facts = []
    canonical = normalize_symbol(symbol)
    price_currency = 'CNY' if canonical.endswith(('.SS', '.SZ', '.BJ')) else 'HKD' if canonical.endswith('.HK') else None
    source_url = f'https://finance.yahoo.com/quote/{normalize_symbol(symbol)}/history/'
    qualifiers = ('auto_adjust=True; adjusted OHLC prices, not unadjusted execution prices',
                  'Completed daily bars only; no invented trading positions')

    def measurement(key, label, value, unit, period, basis):
        missing = value is None or pd.isna(value)
        fact = Fact(key=f'yahoo:{normalize_symbol(symbol)}:{key}', label=label,
                    value=None if missing else Decimal(str(value)), unit=unit, period=period,
                    basis=basis, currency=price_currency if unit == 'price' else None, status='missing' if missing else 'available',
                    qualifiers=qualifiers, source_url=source_url)
        facts.append(fact)
        return fact

    def display(fact, original):
        if fact.value is None:
            return 'N/A'
        return _fmt(float(fact.value)) if isinstance(original, float) else format(fact.value, 'f')

    selected = tuple(dict.fromkeys(indicators or DEFAULT_SNAPSHOT_INDICATORS))
    indicator_values: dict[str, str] = {}
    for name in selected:
        try:
            stock_df[name]  # triggers stockstats calculation
            value = stock_df.iloc[-1][name]
            fact = measurement(f'{df.iloc[-1]["Date"]:%Y-%m-%d}:indicator:{name}', name, value,
                        'index points' if name == 'rsi' else 'price',
                        f'{df.iloc[-1]["Date"]:%Y-%m-%d}', 'stockstats technical indicator')
            indicator_values[name] = display(fact, value)
        except Exception as exc:  # noqa: BLE001 — one bad indicator shouldn't sink the snapshot
            measurement(f'{df.iloc[-1]["Date"]:%Y-%m-%d}:indicator:{name}', name, None,
                        'indicator', f'{df.iloc[-1]["Date"]:%Y-%m-%d}', 'stockstats technical indicator')
            indicator_values[name] = f"N/A ({type(exc).__name__})"

    latest = df.iloc[-1]
    latest_date = _fmt(latest["Date"])
    window = max(1, min(int(look_back_days), 30))
    recent = df.tail(window)

    lines = [
        f"## Verified market data snapshot for {symbol.upper()}",
        "",
        f"- Requested analysis date: {as_of_date}",
        f"- Latest trading row used: {latest_date}",
        daily_bar_note(df).strip(),
        "- Rows after the requested analysis date are excluded before verification.",
        "",
        "### Latest verified OHLCV row",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in ("Open", "High", "Low", "Close", "Volume"):
        fact = measurement(f'{latest_date}:ohlcv:{field}', field, latest.get(field),
                           'source volume units' if field == 'Volume' else 'price', latest_date, 'latest OHLCV')
        lines.append(f"| {field} | {display(fact, latest.get(field))} |")

    lines += ["", "### Verified technical indicators (latest row)", "",
              "| Indicator | Value |", "|---|---:|"]
    for name, value in indicator_values.items():
        lines.append(f"| {name} | {value} |")

    lines += ["", f"### Recent verified closes (last {len(recent)} rows)", "",
              "| Date | Close |", "|---|---:|"]
    for _, row in recent.iterrows():
        day = _fmt(row['Date'])
        fact = measurement(f'{day}:recent_close', 'Close', row.get('Close'), 'price', day, 'daily close')
        lines.append(f"| {day} | {display(fact, row.get('Close'))} |")

    volumes = df.tail(8)
    for _, row in volumes.iterrows():
        day = _fmt(row['Date'])
        measurement(f'{day}:recent_volume', 'Volume', row.get('Volume'), 'source volume units', day, 'daily volume')
    preceding = pd.to_numeric(volumes.iloc[:-1]['Volume'], errors='coerce').dropna()
    if len(preceding) and preceding.mean() > 0 and pd.notna(latest.get('Volume')):
        measurement(f'{latest_date}:volume_ratio', 'Volume comparison',
                    Decimal(str(latest['Volume'])) / (sum(Decimal(str(v)) for v in preceding) / len(preceding)),
                    'ratio', latest_date,
                    f'latest volume / preceding {len(preceding)} available sessions mean')

    lines += [
        "",
        "Use this snapshot as the source of truth for exact OHLCV, price-level, "
        "and indicator-value claims. If another tool output conflicts with it, "
        "flag the discrepancy rather than inventing a reconciled number. Do not "
        "claim historical validation, support/resistance bounces, or exact "
        "percentage moves unless directly supported by tool output with concrete "
        "dates and prices.",
    ]
    return SourceResult("\n".join(lines), tuple(facts))


def build_verified_market_snapshot(symbol: str, as_of_date: str, look_back_days: int = 30,
                                   indicators: Iterable[str] | None = None) -> str:
    return build_verified_market_snapshot_result(symbol, as_of_date, look_back_days, indicators).content
