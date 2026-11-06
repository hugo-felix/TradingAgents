"""All A-share daily-price paths must reject unfinished sessions consistently."""
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.yahoo import market, ohlcv, snapshot

TODAY = '2026-11-05'


def bars(index=False):
    frame = pd.DataFrame({'Date': pd.to_datetime(['2026-11-04', TODAY]),
                          'Open': [100., 900.], 'High': [102., 999.], 'Low': [99., 899.],
                          'Close': [101., 998.], 'Volume': [1000, 99999]})
    return frame.set_index('Date') if index else frame


@pytest.fixture
def noon(monkeypatch):
    # A UTC timestamp must still be interpreted as noon on the Shanghai market date.
    from tradingagents.dataflows import market_session
    now = pd.Timestamp('2026-11-05 04:13:00', tz='UTC')
    monkeypatch.setattr(market_session, '_now', lambda: now)
    return now


def test_price_tool_excludes_intraday_candle(monkeypatch, noon):
    monkeypatch.setattr(market.yf, 'Ticker', lambda _: SimpleNamespace(history=lambda **k: bars(index=True)))
    out = market.get_YFin_data_online('600941.SS', '2026-11-04', TODAY)
    assert '998' not in out and '99999' not in out
    assert '101.00' in out and 'Asia/Shanghai' in out and 'completed' in out.lower()


def test_price_tool_with_only_current_session_reports_unavailable(monkeypatch, noon):
    monkeypatch.setattr(market.yf, 'Ticker', lambda _: SimpleNamespace(history=lambda **k: bars(index=True).tail(1)))
    with pytest.raises(NoMarketDataError, match='completed'):
        market.get_YFin_data_online('600941.SS', TODAY, TODAY)


def test_indicator_loader_uses_same_completed_sessions(monkeypatch, tmp_path, noon):
    monkeypatch.setattr(ohlcv.yf, 'Ticker', lambda _: SimpleNamespace(history=lambda **k: bars(index=True)))
    with run_config({'data_cache_dir': str(tmp_path)}):
        result = ohlcv.load_ohlcv('600941.SS', TODAY)
    assert result['Close'].tolist() == [101.]


def test_verified_snapshot_defensively_excludes_unfinished_session(monkeypatch, noon):
    monkeypatch.setattr(snapshot, 'load_ohlcv', lambda *a, **k: bars())
    out = snapshot.build_verified_market_snapshot('600941.SH', TODAY)
    assert 'Latest trading row used: 2026-11-04' in out
    assert '998' not in out and 'Asia/Shanghai' in out


def test_indicator_does_not_call_unfinished_today_a_holiday(monkeypatch, noon):
    monkeypatch.setattr(market, 'load_ohlcv', lambda *a, **k: bars().head(1))
    out = market.get_stock_stats_indicators_window('600941.SS', 'rsi', TODAY, 1)
    assert 'not finalized' in out


def test_post_close_refetches_a_recent_intraday_cache(monkeypatch, tmp_path):
    from tradingagents.dataflows import market_session
    now = pd.Timestamp('2026-11-05 15:16:00', tz='Asia/Shanghai')
    monkeypatch.setattr(market_session, '_now', lambda: now)
    monkeypatch.setattr(ohlcv.pd.Timestamp, 'today', staticmethod(lambda: now.tz_localize(None)))
    cache = tmp_path / '600941.SS-YFin-data.csv'
    bars().to_csv(cache, index=False)
    written = pd.Timestamp('2026-11-05 15:10:00', tz='Asia/Shanghai').timestamp()
    os.utime(cache, (written, written))
    fetched = []
    settled = bars(index=True)
    settled.loc[pd.Timestamp(TODAY), 'Close'] = 102.
    monkeypatch.setattr(ohlcv.yf, 'Ticker', lambda _: SimpleNamespace(history=lambda **k: fetched.append(True) or settled))
    with run_config({'data_cache_dir': str(tmp_path)}):
        result = ohlcv.load_ohlcv('600941.SS', TODAY)
    assert fetched and result.iloc[-1]['Close'] == 102.


def test_session_policy_leaves_overseas_prices_unchanged(monkeypatch, noon):
    monkeypatch.setattr(market.yf, 'Ticker', lambda _: SimpleNamespace(history=lambda **k: bars(index=True)))
    assert '998.00' in market.get_YFin_data_online('AAPL', '2026-11-04', TODAY)


def test_a_share_download_end_uses_market_date_not_host_date(monkeypatch, tmp_path):
    from tradingagents.dataflows import market_session
    monkeypatch.setattr(market_session, '_now', lambda: pd.Timestamp('2026-11-05 15:16', tz='Asia/Shanghai'))
    monkeypatch.setattr(ohlcv.pd.Timestamp, 'today', staticmethod(lambda: pd.Timestamp('2026-11-04 23:16')))
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return bars(index=True)

    monkeypatch.setattr(ohlcv.yf, 'Ticker', lambda _: SimpleNamespace(history=download))
    with run_config({'data_cache_dir': str(tmp_path)}):
        ohlcv.load_ohlcv('600941.SS', TODAY)
    assert calls[0]['end'] == '2026-11-06'


def test_a_share_cache_time_is_compared_in_market_timezone(monkeypatch, tmp_path):
    from tradingagents.dataflows import market_session
    now = pd.Timestamp('2026-11-05 15:16:10', tz='Asia/Shanghai')
    monkeypatch.setattr(market_session, '_now', lambda: now)
    monkeypatch.setattr(ohlcv.pd.Timestamp, 'today', staticmethod(lambda: pd.Timestamp('2026-11-04 23:16:10')))
    cache = tmp_path / '600941.SS-YFin-data.csv'
    bars().to_csv(cache, index=False)
    written = (now - pd.Timedelta(seconds=10)).timestamp()
    os.utime(cache, (written, written))
    monkeypatch.setattr(ohlcv.yf, 'Ticker', Mock(side_effect=AssertionError('fresh post-close cache must be reused')))
    with run_config({'data_cache_dir': str(tmp_path)}):
        assert ohlcv.load_ohlcv('600941.SS', TODAY).iloc[-1]['Close'] == 998.
