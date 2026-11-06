"""The data tools the analysts call.

Each dated tool takes the run's ``trade_date`` from graph state (``InjectedState``)
and never serves data past it, whatever date the model asks for.
"""

from typing import Annotated, Literal

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from tradingagents.dataflows.date_window import as_of, as_of_window
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.facts import SourceResult
from tradingagents.dataflows.router import (
    no_data_available,
    route_to_vendor,
    route_to_vendor_result,
    vendor_unavailable,
)
from tradingagents.dataflows.vendors.yahoo.snapshot import build_verified_market_snapshot_result


@tool
def get_stock_data(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve stock price data (OHLCV) for the instrument under analysis.
    Uses the configured core_stock_apis vendor.
    Args:
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the price data for the instrument over the date range.
    """
    try:
        start_date, end_date = as_of_window(start_date, end_date, trade_date)
    except ValueError as e:
        return str(e)
    return route_to_vendor("get_stock_data", symbol, start_date, end_date)


@tool
def get_indicators(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve a single technical indicator for the instrument under analysis.
    Uses the configured technical_indicators vendor.
    Args:
        indicator (str): A single technical indicator name, e.g. 'rsi', 'macd'. Call this tool once per indicator.
        curr_date (str): The current trading date you are trading on, YYYY-mm-dd
        look_back_days (int): How many days to look back, default is 30
    Returns:
        str: A formatted dataframe containing the technical indicators for the instrument and indicator.
    """
    # LLMs sometimes pass multiple indicators as a comma-separated string;
    # split and process each individually.
    curr_date = as_of(curr_date, trade_date)
    indicators = [i.strip().lower() for i in indicator.split(",") if i.strip()]
    results = []
    for ind in indicators:
        try:
            results.append(route_to_vendor("get_indicators", symbol, ind, curr_date, look_back_days))
        except ValueError as e:
            results.append(str(e))
    return "\n\n".join(results)


@tool(response_format="content_and_artifact")
def get_verified_market_snapshot(
    symbol: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "the current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[
        int, "number of recent trading rows to include for sanity-checking"
    ] = 30,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> tuple[str, dict]:
    """Deterministic verification snapshot for exact market-data claims.

    Returns the latest OHLCV row on or before curr_date, common technical
    indicators, and recent closes. Call this before making exact claims about
    price levels, Bollinger bands, RSI, MACD, moving averages, support /
    resistance, or historical comparisons, and treat it as the source of truth.
    """
    # An exception out of a tool would end the run.
    try:
        return build_verified_market_snapshot_result(symbol, as_of(curr_date, trade_date), look_back_days).as_tool_output()
    except VendorUnavailableError as exc:
        return SourceResult(vendor_unavailable("get_verified_market_snapshot", exc)).as_tool_output()
    except NoMarketDataError as exc:
        return SourceResult(no_data_available(exc)).as_tool_output()


@tool
def get_fundamentals(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve comprehensive fundamental data for the instrument under analysis.
    Uses the configured fundamental_data vendor.
    Args:
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing comprehensive fundamental data
    """
    return route_to_vendor("get_fundamentals", ticker, as_of(curr_date, trade_date))


@tool(response_format="content_and_artifact")
def get_balance_sheet(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    freq: Annotated[Literal["annual", "quarterly"], "reporting frequency"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> tuple[str, dict]:
    """
    Retrieve balance sheet data for the instrument under analysis.
    Uses the configured fundamental_data vendor.
    Args:
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing balance sheet data
    """
    return route_to_vendor_result("get_balance_sheet", ticker, freq, as_of(curr_date, trade_date)).as_tool_output()


@tool(response_format="content_and_artifact")
def get_cashflow(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    freq: Annotated[Literal["annual", "quarterly"], "reporting frequency"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> tuple[str, dict]:
    """
    Retrieve cash flow statement data for the instrument under analysis.
    Uses the configured fundamental_data vendor.
    Args:
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing cash flow statement data
    """
    return route_to_vendor_result("get_cashflow", ticker, freq, as_of(curr_date, trade_date)).as_tool_output()


@tool(response_format="content_and_artifact")
def get_income_statement(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    freq: Annotated[Literal["annual", "quarterly"], "reporting frequency"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> tuple[str, dict]:
    """
    Retrieve income statement data for the instrument under analysis.
    Uses the configured fundamental_data vendor.
    Args:
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing income statement data
    """
    return route_to_vendor_result("get_income_statement", ticker, freq, as_of(curr_date, trade_date)).as_tool_output()


@tool(response_format="content_and_artifact")
def get_company_disclosures(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "Analysis date in YYYY-MM-DD format"] = None,
    look_back_days: Annotated[int, "Disclosure lookback, default 730 days for comparable periods"] = 730,
    limit: Annotated[int, "Maximum official PDFs to read, default 6"] = 6,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> tuple[str, dict]:
    """Read dated mainland A-share official disclosures and operating metrics.

    Returns source-linked PDF excerpts and, where supported, metrics with their
    reporting periods and units. Company guidance is not an achieved result.
    Use this for formal announcements, customer data and dividend disclosures.
    """
    return route_to_vendor_result("get_company_disclosures", ticker, as_of(curr_date, trade_date), look_back_days, limit).as_tool_output()


@tool(response_format="content_and_artifact")
def get_company_operating_history(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "Analysis date in YYYY-MM-DD format"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> tuple[str, dict]:
    """Read supported A-share issuer operating history and actual capital expenditure.

    Currently supports China Mobile's verified official layouts. Returns dated,
    source-linked capex, H1 ARPU and quarterly customer records with units and
    comparability gaps. Do not annualize half-year capex or splice different
    broadband definitions. A statement date is not necessarily publication time.
    """
    return route_to_vendor_result("get_company_operating_history", ticker, as_of(curr_date, trade_date)).as_tool_output()


@tool(response_format="content_and_artifact")
def get_valuation_comparison(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "Analysis date in YYYY-MM-DD format"] = None,
    look_back_years: Annotated[Literal[1, 3, 5], "Historical valuation window; actual coverage may be shorter"] = 3,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> tuple[str, dict]:
    """Read A-share historical PE(TTM)/PB percentiles and supported same-day peers.

    Eastmoney is the primary source; the three telecom A-share issuers have an
    explicit peer group. Percentiles use positive finite observations and state
    their dates and sample counts. They do not establish fair value. Source
    coverage and accounting bases can differ; do not mix A-share/H-share data.
    """
    return route_to_vendor_result("get_valuation_comparison", ticker, as_of(curr_date, trade_date), look_back_years).as_tool_output()


@tool(response_format="content_and_artifact")
def get_dividends(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    curr_date: Annotated[str, "Analysis date in YYYY-MM-DD format"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> tuple[str, dict]:
    """Read A-share dividend schemes, gross cash per share, dates and scheme stage.

    Confirm amounts and policy against official disclosures. A proposed scheme
    is not a completed payment, and A-share/H-share currencies can differ.
    """
    return route_to_vendor_result("get_dividends", ticker, as_of(curr_date, trade_date)).as_tool_output()


@tool
def get_news(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve news data for the instrument under analysis.
    Uses the configured news_data vendor.
    Args:
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted string containing news data
    """
    try:
        start_date, end_date = as_of_window(start_date, end_date, trade_date)
    except ValueError as e:
        return str(e)
    return route_to_vendor("get_news", ticker, start_date, end_date)


@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int | None, "Days to look back; omit to use the configured default"] = None,
    limit: Annotated[int | None, "Max articles to return; omit to use the configured default"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve global news data.
    Uses the configured news_data vendor. Defaults for look_back_days and
    limit come from DEFAULT_CONFIG (global_news_lookback_days,
    global_news_article_limit); pass explicit values to override.

    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back; omit to inherit config
        limit (int): Maximum number of articles to return; omit to inherit config

    Returns:
        str: A formatted string containing global news data
    """
    return route_to_vendor("get_global_news", as_of(curr_date, trade_date), look_back_days, limit)


@tool
def get_insider_transactions(
    ticker: Annotated[str, InjectedState("company_of_interest")],
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve insider transaction information about a company.
    Uses the configured news_data vendor.
    Returns:
        str: A report of insider transaction data
    """
    return route_to_vendor("get_insider_transactions", ticker, trade_date or None)


@tool
def get_macro_indicators(
    indicator: Annotated[
        str,
        "Macro indicator: a friendly alias such as 'cpi', 'core_pce', "
        "'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve', "
        "'real_gdp', 'vix', euro-area aliases such as 'ecb_deposit_rate', "
        "'euro_hicp', 'euro_core_hicp', 'euro_real_gdp', 'germany_10y', "
        "'france_10y', 'eur_usd', or a raw FRED series ID such as 'CPIAUCSL'. "
        "For China use explicit china_cpi, china_pmi, china_gdp, china_lpr or china_credit.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the window"],
    look_back_days: Annotated[
        int | None, "Trailing window length in days; omit for a 1-year window"
    ] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve a macroeconomic time series. Explicit china_cpi, china_pmi,
    china_gdp, china_lpr and china_credit identifiers use AKShare China data;
    cpi and other unprefixed aliases continue to identify the existing FRED
    US/euro-area series. Never substitute one country's data for another.
    Returns the series title, units, frequency, the latest value, the change
    over the window, and a recent observation table. Uses the configured
    macro_data vendor.

    Args:
        indicator (str): Friendly alias or raw FRED series ID
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Trailing window length; omit for a 1-year window

    Returns:
        str: A formatted markdown report of the macro series
    """
    return route_to_vendor("get_macro_indicators", indicator, as_of(curr_date, trade_date), look_back_days)


@tool
def get_prediction_markets(
    topic: Annotated[
        str,
        "Event topic/keyword, e.g. 'Fed rate cut', 'recession 2026', "
        "'US election', or a sector/company event.",
    ],
    limit: Annotated[int | None, "Max markets to return; omit for a default of 6"] = None,
    trade_date: Annotated[str, InjectedState("trade_date")] = "",
) -> str:
    """
    Retrieve live, market-implied probabilities for forward-looking events from
    prediction markets (Polymarket): Fed decisions, recession, elections,
    geopolitics, crypto. Returns the most-traded open markets matching the
    topic, each with its implied probability, traded volume, resolution date,
    and recent move. Uses the configured prediction_markets vendor.

    Args:
        topic (str): Event keyword(s) to search
        limit (int): Max markets to return; omit for a default of 6

    Returns:
        str: A formatted markdown report of matching prediction markets
    """
    return route_to_vendor("get_prediction_markets", topic, limit, trade_date or None)
