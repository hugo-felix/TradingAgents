"""Dated price-based context for the income watchlist, not a trading signal."""
from __future__ import annotations

import math
import statistics
from datetime import date

from tradingagents.dataflows.errors import VendorUnavailableError

INDEX_HISTORY = 'index_zh_a_hist'
INDEX_FALLBACK = 'stock_zh_index_daily_tx'
SECTOR_HISTORY = 'stock_board_industry_hist_em'
METHOD = ('研究预设，未经回测：收盘低于120日均线且均线较20个交易日前下降，视为趋势偏弱；'
          '20日年化波动率>30%或近60日最大回撤>15%，视为风险升高。'
          '行业另比较同期60日涨幅与沪深300，落后超过5个百分点提示相对偏弱。'
          '只评估价格环境，不代表行业盈利、估值或分红前景；不产生买卖指令。')


def price_context(rows, asof):
    """Strictly completed bars; reject duplicates, gaps and stale/short history."""
    points = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row['日期'])[:10])
            if day >= asof:
                continue
            close = float(row['收盘'])
            if not math.isfinite(close) or close <= 0 or day in points:
                raise ValueError
            points[day] = close
        except (KeyError, TypeError, ValueError, OverflowError):
            return {'status': 'unknown', 'reason': '行情日期、价格或重复记录异常'}
    ordered = sorted(points)
    if len(ordered) < 140:
        return {'status': 'unknown', 'reason': '不足140个完整交易日'}
    if (asof - ordered[-1]).days > 4:
        return {'status': 'unknown', 'reason': '最新完整行情距筛选日超过4个自然日'}
    if any((b - a).days > 15 for a, b in zip(ordered[-140:-1], ordered[-139:], strict=True)):
        return {'status': 'unknown', 'reason': '行情存在超过15个自然日的间断'}
    closes = [points[day] for day in ordered]
    sma = statistics.mean(closes[-120:])
    previous_sma = statistics.mean(closes[-140:-20])
    returns = [b / a - 1 for a, b in zip(closes[-21:-1], closes[-20:], strict=True)]
    volatility = statistics.stdev(returns) * math.sqrt(252) * 100
    # Sixty trading-day returns require the starting close plus sixty closes.
    peak, drawdown = closes[-61], 0.0
    for value in closes[-61:]:
        peak = max(peak, value)
        drawdown = max(drawdown, (1 - value / peak) * 100)
    weak = closes[-1] < sma and sma < previous_sma
    stress = volatility > 30 or drawdown > 15
    return {
        'status': 'caution' if weak or stress else 'normal',
        'reason': '；'.join(filter(None, [
            '收盘低于120日均线，且均线下行' if weak else '',
            f'20日年化波动率{volatility:.2f}% > 30%' if volatility > 30 else '',
            f'60日最大回撤{drawdown:.2f}% > 15%' if drawdown > 15 else '',
        ])) or '价格风险未触及预设警戒线',
        'as_of': ordered[-1].isoformat(), 'return_start': ordered[-61].isoformat(),
        'close': closes[-1], 'sma_120': sma,
        'sma_slope_20_pct': (sma / previous_sma - 1) * 100,
        'return_60d_pct': (closes[-1] / closes[-61] - 1) * 100,
        'volatility_20d_pct': volatility, 'drawdown_60d_pct': drawdown,
    }


def collect_market_context(source, items, asof):
    """Reuse the screen's bounded/cached vendor worker. Failures stay unknown."""
    def collect(endpoint, symbol):
        try:
            packet = source.fetch(endpoint, symbol)
            rows = packet['rows']
            if endpoint == INDEX_FALLBACK:
                rows = [{'日期': row.get('date'), '收盘': row.get('close')} for row in rows]
            context = price_context(rows, asof)
            context['fetched_at'] = packet['fetched_at']
        except VendorUnavailableError:
            context = {'status': 'unknown', 'reason': '行情接口不可用，请刷新后重试'}
        return {**context, 'endpoint': endpoint, 'symbol': symbol}

    market = collect(INDEX_HISTORY, '000300')
    if market['status'] == 'unknown':
        fallback = collect(INDEX_FALLBACK, 'sh000300')
        if fallback['status'] != 'unknown':
            market = {**fallback, 'fallback_from': INDEX_HISTORY}
    sectors = {}
    for sector in sorted({item['sector'] for item in items
                          if item.get('status') == 'pass'
                          and (item.get('core') or {}).get('status') == 'pass'}):
        context = collect(SECTOR_HISTORY, sector)
        if (context.get('return_start') and context.get('return_start') == market.get('return_start')
                and context.get('as_of') == market.get('as_of')):
            relative = context['return_60d_pct'] - market['return_60d_pct']
            context['relative_60d_pct'] = relative
            if relative < -5:
                context.update(status='caution', reason=context['reason'] + '；60日落后沪深300超过5个百分点')
        elif context.get('status') == 'normal':
            context.update(status='unknown', reason='行业与沪深300的完整行情期间不一致，无法确认相对表现')
        sectors[sector] = context
    return {'version': 1, 'as_of': asof.isoformat(), 'method': METHOD,
            'market': {**market, 'name': '沪深300'}, 'sectors': sectors}
