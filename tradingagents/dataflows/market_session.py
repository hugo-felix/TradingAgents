"""A-share daily data uses completed sessions, in the exchange's own time zone.

A current session is eligible after 15:15 Shanghai time, allowing fifteen
minutes after the regular close for the daily feed to publish. Trading dates
come from the returned bars, so holidays use the last available session.
"""
from datetime import time

import pandas as pd

from tradingagents.dataflows.symbols import is_a_share

TIMEZONE = 'Asia/Shanghai'
DAILY_FEED_READY = time(15, 15)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz=TIMEZONE)


def market_time() -> pd.Timestamp:
    return _now().tz_convert(TIMEZONE)


def session_not_finalized(symbol: str, day: str) -> bool:
    if not is_a_share(symbol):
        return False
    now = market_time()
    return pd.Timestamp(day).date() == now.date() and now.time() < DAILY_FEED_READY


def _local_day(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(TIMEZONE).tz_localize(None)
    return stamp.normalize()


def completed_daily_bars(data: pd.DataFrame, symbol: str, as_of_date: str) -> pd.DataFrame:
    """Remove future and unfinished A-share sessions from either OHLCV layout."""
    if not is_a_share(symbol):
        return data
    now = market_time()
    cutoff = min(pd.Timestamp(as_of_date).normalize(), now.tz_localize(None).normalize())
    if cutoff.date() == now.date() and now.time() < DAILY_FEED_READY:
        cutoff -= pd.Timedelta(days=1)
    raw_dates = data['Date'] if 'Date' in data else pd.Series(data.index, index=data.index)
    dates = raw_dates.map(_local_day)
    result = data.loc[dates <= cutoff].copy()
    result.attrs['market_as_of'] = now.isoformat()
    result.attrs['daily_bar_policy'] = 'completed A-share sessions only'
    result.attrs['market_timezone'] = TIMEZONE
    return result


def daily_bar_note(data: pd.DataFrame) -> str:
    if 'daily_bar_policy' not in data.attrs:
        return ''
    return (f"# Market data as of: {data.attrs['market_as_of']}\n"
            f"# Daily-bar policy: {data.attrs['daily_bar_policy']}; {TIMEZONE}. "
            'The current session is excluded before 15:15 local time.\n')


def cache_predates_completed_session(written_seconds: float, symbol: str, as_of_date: str) -> bool:
    """Invalidate a pre-close download even if its normal short TTL has not expired."""
    if not is_a_share(symbol):
        return False
    now = market_time()
    if pd.Timestamp(as_of_date).date() < now.date() or now.time() < DAILY_FEED_READY:
        return False
    written = pd.Timestamp(written_seconds, unit='s', tz='UTC').tz_convert(TIMEZONE)
    ready = now.normalize() + pd.Timedelta(hours=15, minutes=15)
    return written < ready
