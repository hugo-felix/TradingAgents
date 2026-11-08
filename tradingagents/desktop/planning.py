"""Personal preparation limits and arithmetic, without trading recommendations."""

from __future__ import annotations

import math
from datetime import date
from decimal import ROUND_DOWN, Decimal


def validate_preferences(values):
    if not isinstance(values, dict):
        raise ValueError('个人计划格式无效。')
    fields = {
        'target_yield': ('目标税前股息率', 4, 30),
        'capital': ('计划投入资金', 1, 1_000_000_000),
        'position_pct': ('单只投入上限', 1, 100),
        'horizon_years': ('预计持有年限', 1, 50),
    }
    result = {}
    for key, (label, minimum, maximum) in fields.items():
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f'请填写{label}。')
        try:
            number = float(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f'{label}格式无效。') from exc
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError(f'{label}应在{minimum}至{maximum}之间。')
        result[key] = number
    return result


def entry_candidates(snapshot, preferences):
    """Only include verified annual-yield/core passes; missing DPS stays missing."""
    rows = []
    for item in (snapshot or {}).get('items', []):
        metrics = item['metrics']
        yield_pct = metrics.get('yield_pct')
        if (item.get('status') != 'pass' or (item.get('core') or {}).get('status') != 'pass'
                or not isinstance(yield_pct, (int, float)) or not math.isfinite(yield_pct)
                or yield_pct <= 4):
            continue
        dps = metrics.get('dividend_annual')
        reference = None
        if (preferences and isinstance(dps, (int, float))
                and math.isfinite(dps) and dps > 0):
            # Round downward so the displayed cent never understates the requested yield.
            reference = float((Decimal(str(dps)) * 100
                               / Decimal(str(preferences['target_yield'])))
                              .quantize(Decimal('.01'), rounding=ROUND_DOWN))
        rows.append({
            'code': item['code'], 'name': item['name'], 'sector': item['sector'],
            'metrics': metrics, 'reference_price': reference,
        })
    return sorted(rows, key=lambda row: (-row['metrics']['yield_pct'], row['code']))


def _positive(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def strategy_plan(snapshot, preferences, holdings, today=None):
    """Explain research readiness; never translate a rating into an order."""
    today = today or date.today()
    snapshot = snapshot or {}
    context = snapshot.get('market_context') or {}
    try:
        asof = date.fromisoformat(snapshot.get('as_of', ''))
        fresh = 0 <= (today - asof).days <= 4
    except (TypeError, ValueError):
        fresh = False

    def environment(value):
        value = value or {}
        try:
            day = date.fromisoformat(value.get('as_of', ''))
            valid = (fresh and context.get('as_of') == snapshot.get('as_of')
                     and day < asof and 0 <= (today - day).days <= 4)
        except (TypeError, ValueError):
            valid = False
        if not valid or value.get('status') not in ('normal', 'caution'):
            return {**value, 'status': 'unknown',
                    'reason': value.get('reason') if fresh and value.get('status') == 'unknown'
                    else '行情环境缺失、过期或日期不一致，请刷新高股息清单'}
        return value

    market = environment(context.get('market'))
    sectors = {key: environment(value) for key, value in context.get('sectors', {}).items()}
    lookup = {item['code']: item for item in snapshot.get('items', [])}
    exposure, missing = {}, []
    active = [holding for holding in holdings if _positive(holding.get('quantity'))]
    for holding in active:
        code = holding['ticker'].split('.')[0]
        item = lookup.get(code) or {}
        price = (item.get('metrics') or {}).get('price')
        sector = item.get('sector')
        if not fresh or not _positive(price) or not sector or sector in ('未知', '待核验', '其他'):
            missing.append(code)
            continue
        exposure[sector] = exposure.get(sector, 0) + price * holding['quantity']
    total = sum(exposure.values())
    portfolio = {
        'status': 'unknown' if missing else 'available' if active else 'empty',
        'missing_codes': missing,
        'sectors': ([{'sector': key, 'weight_pct': value / total * 100}
                     for key, value in sorted(exposure.items(), key=lambda pair: -pair[1])]
                    if total and not missing else []),
        'basis': '仅已录入股票的快照市值占比，不含现金及外部资产；不是总资产仓位',
    }
    candidates = entry_candidates(snapshot, preferences)
    for row in candidates:
        sector = sectors.get(row['sector']) or environment(None)
        reasons = []
        if market['status'] == 'caution':
            reasons.append('大盘价格风险升高，暂停新增计划并复核')
        if sector['status'] == 'caution':
            reasons.append('行业趋势或相对表现偏弱，先核对行业及公司最新公告')
        if reasons:
            status, label = 'wait', '先等待 / 复核风险'
        elif market['status'] == 'unknown' or sector['status'] == 'unknown':
            status, label = 'data', '先补环境数据'
            reasons.append('大盘或行业价格环境尚未确认，刷新清单后再评估')
        elif portfolio['status'] == 'unknown':
            status, label = 'data', '先补持仓数据'
            reasons.append('已有持仓无法完整计价，尚不能确认行业集中度')
        elif not preferences:
            status, label = 'plan', '先设置个人计划'
        elif row['reference_price'] is None or not _positive(row['metrics'].get('price')):
            status, label = 'data', '先补分红或价格'
        elif row['metrics']['price'] > row['reference_price']:
            status, label = 'price', '等待目标股息价格'
            reasons.append('快照价格高于个人股息目标对应价格，交易前用实时行情复算')
        else:
            status, label = 'research', '可进入研究复核'
            reasons.append('仅价格环境与股息目标满足预设；仍需估值、最新公告和研究条件确认')
        weights = {item['sector']: item['weight_pct'] for item in portfolio['sectors']}
        row['strategy'] = {'status': status, 'label': label, 'reasons': reasons,
                           'sector': sector, 'held_sector_pct': weights.get(row['sector']),
                           'portfolio_status': portfolio['status']}
    return {'market': market, 'portfolio': portfolio, 'candidates': candidates,
            'method': context.get('method') or '刷新高股息清单后评估大盘与候选行业价格环境。',
            'limitations': '行业整体盈利与估值分位尚未评估。价格环境正常不代表可以买入；'
                            '开盘前复核公告及实时价格，分批执行自己的计划，条件失效时重新研究。'}
