"""Local security identities, independent of model-generated report prose."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files

from tradingagents.dataflows.symbols import normalize_symbol


@lru_cache(maxsize=1)
def stock_names() -> dict[str, str]:
    data = files('tradingagents.desktop') / 'data' / 'a-share-names.json'
    return json.loads(data.read_text(encoding='utf-8'))['names']


@lru_cache(maxsize=1)
def _reverse_names():
    return {name: ticker for ticker, name in stock_names().items()}


def canonical_symbol(raw: str) -> str:
    value = str(raw).strip().upper()
    if value in _reverse_names():
        return _reverse_names()[value]
    if match := re.fullmatch(r'(SH|SZ|BJ)[.\s]?(\d{6})', value):
        value = match[2] + '.' + {'SH': 'SS', 'SZ': 'SZ', 'BJ': 'BJ'}[match[1]]
    if re.fullmatch(r'\d{6}', value):
        for suffix in ('SS', 'SZ', 'BJ'):
            candidate = value + '.' + suffix
            if candidate in stock_names():
                return candidate
        if value.startswith(('6', '5', '9')) and not value.startswith('920'):
            value += '.SS'
        elif value.startswith(('0', '1', '2', '3')):
            value += '.SZ'
        elif value.startswith(('4', '8', '920')):
            value += '.BJ'
    return normalize_symbol(value)


def stock_identity(raw: str) -> dict:
    ticker = canonical_symbol(raw)
    name = stock_names().get(ticker, '')
    suffix = ticker.rsplit('.', 1)[-1] if '.' in ticker else ''
    return {'ticker': ticker, 'code': ticker.split('.')[0], 'name': name, 'resolved': bool(name),
            'exchange': {'SS': '上海证券交易所', 'SZ': '深圳证券交易所', 'BJ': '北京证券交易所',
                         'HK': '香港交易所'}.get(suffix, ''),
            'source': 'exchange_directory' if name else ''}
