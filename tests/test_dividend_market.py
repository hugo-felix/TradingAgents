from datetime import date, timedelta

import pytest

from tradingagents.dataflows.dividend_market import (
    INDEX_FALLBACK,
    INDEX_HISTORY,
    collect_market_context,
    price_context,
)
from tradingagents.dataflows.errors import VendorUnavailableError

ASOF = date(2026, 11, 8)


def bars(direction=1):
    days = []
    day = ASOF - timedelta(days=1)
    while len(days) < 160:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return [{'日期': day.isoformat(), '收盘': 200 + direction * i / 5}
            for i, day in enumerate(reversed(days))]


def test_price_context_uses_only_completed_bars():
    rows = bars()
    result = price_context(rows, ASOF)
    assert result['status'] == 'normal'
    assert result['as_of'] == '2026-11-06'
    assert result['drawdown_60d_pct'] == 0
    assert result['return_60d_pct'] == pytest.approx((231.8 / 219.8 - 1) * 100)
    assert price_context(rows + [{'日期': ASOF.isoformat(), '收盘': None}], ASOF) == result
    assert price_context(bars(-1), ASOF)['status'] == 'caution'


def test_drawdown_includes_start_of_sixty_day_return_window():
    rows = bars()
    for row in rows:
        row['收盘'] = 100
    rows[-61]['收盘'] = 120
    result = price_context(rows, ASOF)
    assert result['drawdown_60d_pct'] == pytest.approx(100 / 6)
    assert result['status'] == 'caution'
    assert '60日最大回撤16.67% > 15%' in result['reason']
    assert '年化波动率' not in result['reason']


def test_volatility_warning_identifies_trigger_without_drawdown_alarm():
    rows = bars()
    for i, row in enumerate(rows):
        row['收盘'] = 100 if i % 2 else 104
    result = price_context(rows, ASOF)
    assert result['volatility_20d_pct'] > 30
    assert result['drawdown_60d_pct'] < 15
    assert '20日年化波动率' in result['reason']
    assert '60日最大回撤' not in result['reason']


@pytest.mark.parametrize('problem', ['short', 'stale', 'duplicate', 'invalid', 'gap'])
def test_price_context_rejects_unreliable_history(problem):
    rows = bars()
    if problem == 'short':
        rows = rows[-139:]
    elif problem == 'stale':
        rows = rows[:-5]
    elif problem == 'duplicate':
        rows.append(rows[-1])
    elif problem == 'invalid':
        rows[-1]['收盘'] = float('nan')
    else:
        # Keep 140 observations but introduce a long unexplained hole.
        for row in rows[:80]:
            row['日期'] = (date.fromisoformat(row['日期']) - timedelta(days=30)).isoformat()
    assert price_context(rows, ASOF)['status'] == 'unknown'


def test_collection_reuses_source_and_compares_identical_periods():
    calls = []

    class Source:
        def fetch(self, endpoint, symbol):
            calls.append((endpoint, symbol))
            return {'rows': bars(1 if endpoint == INDEX_HISTORY else -1),
                    'fetched_at': '2026-11-08T08:00:00+08:00'}

    item = {'sector': '银行', 'status': 'pass', 'core': {'status': 'pass'}}
    result = collect_market_context(Source(), [item, item], ASOF)
    assert len(calls) == 2
    assert result['sectors']['银行']['relative_60d_pct'] < -5
    assert result['sectors']['银行']['status'] == 'caution'


def test_vendor_failure_is_unknown_not_market_weakness():
    class Source:
        def fetch(self, endpoint, symbol):
            raise VendorUnavailableError('sensitive provider diagnostic')

    result = collect_market_context(Source(), [], ASOF)
    assert result['market']['status'] == 'unknown'
    assert 'sensitive' not in result['market']['reason']


def test_missing_benchmark_cannot_confirm_sector_relative_strength():
    class Source:
        def fetch(self, endpoint, symbol):
            if endpoint in (INDEX_HISTORY, INDEX_FALLBACK):
                raise VendorUnavailableError('unavailable')
            return {'rows': bars(), 'fetched_at': '2026-11-08T08:00:00+08:00'}

    item = {'sector': '银行', 'status': 'pass', 'core': {'status': 'pass'}}
    sector = collect_market_context(Source(), [item], ASOF)['sectors']['银行']
    assert sector['status'] == 'unknown'
    assert 'relative_60d_pct' not in sector


def test_index_fallback_keeps_actual_source_and_period():
    class Source:
        def fetch(self, endpoint, symbol):
            if endpoint == INDEX_HISTORY:
                raise VendorUnavailableError('unavailable')
            rows = bars()
            if endpoint == INDEX_FALLBACK:
                assert symbol == 'sh000300'
                rows = [{'date': r['日期'], 'close': r['收盘']} for r in rows]
            return {'rows': rows, 'fetched_at': '2026-11-08T08:00:00+08:00'}
    result = collect_market_context(Source(), [
        {'sector': '银行', 'status': 'pass', 'core': {'status': 'pass'}}], ASOF)
    assert result['market']['endpoint'] == INDEX_FALLBACK
    assert result['market']['fallback_from'] == INDEX_HISTORY
    assert result['market']['status'] == 'normal'
    assert result['sectors']['银行']['relative_60d_pct'] == 0
