"""Optional AKShare SDK access and mainland exchange symbol conversion."""

import importlib

import pandas as pd

from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.symbols import is_a_share, normalize_symbol


def resolve_symbol(ticker: str) -> tuple[str, str]:
    canonical = normalize_symbol(ticker)
    if not is_a_share(canonical):
        raise NoMarketDataError(ticker, canonical, "AKShare covers exchange-qualified mainland A-share stocks only")
    code = canonical.split(".", 1)[0]
    market = {"SS": "SH", "SZ": "SZ", "BJ": "BJ"}[canonical.rsplit(".", 1)[1]]
    return code, market + code


def request(endpoint: str, **params) -> pd.DataFrame:
    try:
        sdk = importlib.import_module("akshare")
    except ImportError as exc:
        raise VendorNotConfiguredError("AKShare is not installed; run pip install '.[a-shares]'") from exc
    try:
        result = getattr(sdk, endpoint)(**params)
    except Exception as exc:
        raise VendorUnavailableError(f"AKShare {endpoint} failed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(result, pd.DataFrame):
        raise VendorUnavailableError(f"AKShare {endpoint} returned an unexpected response")
    return result
