"""A-share financial statements with explicit periods, units and comparisons."""

from decimal import Decimal

import pandas as pd

from tradingagents.dataflows import date_window
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.facts import Fact, SourceResult

from . import common
from .financial_metrics import calculate_statement_metrics

_STATEMENTS = {
    "balance_sheet": (
        "stock_balance_sheet_by_report_em", "Balance Sheet",
        {"TOTAL_ASSETS": "Total Assets", "TOTAL_LIABILITIES": "Total Liabilities",
         "TOTAL_PARENT_EQUITY": "Parent Equity", "TOTAL_CURRENT_ASSETS": "Current Assets",
         "TOTAL_CURRENT_LIAB": "Current Liabilities", "MONETARYFUNDS": "Cash and Bank Balances",
         "ACCOUNTS_RECE": "Accounts Receivable", "INVENTORY": "Inventory",
         "SHORT_LOAN": "Short-term Borrowings", "LONG_LOAN": "Long-term Borrowings"},
        ("TOTAL_ASSETS", "TOTAL_LIABILITIES"),
    ),
    "income_statement": (
        "stock_profit_sheet_by_report_em", "Income Statement",
        {"OPERATE_INCOME": "Operating Revenue", "TOTAL_OPERATE_INCOME": "Total Operating Revenue",
         "OPERATE_COST": "Operating Cost", "OPERATE_PROFIT": "Operating Profit",
         "TOTAL_PROFIT": "Profit Before Tax", "NETPROFIT": "Net Profit",
         "PARENT_NETPROFIT": "Parent Net Profit", "DEDUCT_PARENT_NETPROFIT": "Adjusted Parent Net Profit",
         "BASIC_EPS": "Basic EPS (source currency/share)", "DILUTED_EPS": "Diluted EPS (source currency/share)"},
        ("OPERATE_INCOME", "PARENT_NETPROFIT"),
    ),
    "cashflow": (
        "stock_cash_flow_sheet_by_report_em", "Cash Flow Statement",
        {"NETCASH_OPERATE": "Operating Cash Flow", "NETCASH_INVEST": "Investing Cash Flow (net)",
         "NETCASH_FINANCE": "Financing Cash Flow", "SALES_SERVICES": "Cash from Sales and Services",
         "CONSTRUCT_LONG_ASSET": "购建长期资产支付现金",
         "ASSIGN_DIVIDEND_PORFIT": "Dividends/Profit Distributions and Interest Paid", "CCE_ADD": "Net Change in Cash",
         "BEGIN_CCE": "Beginning Cash Equivalents", "END_CCE": "Ending Cash Equivalents"},
        ("NETCASH_OPERATE", "NETCASH_INVEST", "NETCASH_FINANCE"),
    ),
}


def _statement_result(kind: str, ticker: str, freq: str, as_of_date: str | None) -> SourceResult:
    _, symbol = common.resolve_symbol(ticker)
    if freq not in ("annual", "quarterly"):
        raise ValueError("freq must be annual or quarterly")
    endpoint, title, fields, required = _STATEMENTS[kind]
    as_of_date = as_of_date or date_window.get_current_date()
    cutoff = pd.Timestamp(as_of_date).normalize()
    if date_window.is_historical(as_of_date):
        # NOTICE_DATE alone is insufficient: a later restatement may retain the
        # original notice date. AKShare has no complete as-filed revision history.
        return SourceResult(
            f"# {title} for {ticker} (AKShare)\n"
            f"# Point-in-time as of: {as_of_date}\n\n"
            "Statements are withheld for this historical date: AKShare serves current "
            "values without complete revision history, so values as originally filed "
            "cannot be verified. Do not substitute today's restated figures."
        )
    data = common.request(endpoint, symbol=symbol)
    if data.empty:
        raise NoMarketDataError(ticker, symbol, f"no {title.lower()} rows")
    if not {"REPORT_DATE", "NOTICE_DATE", *required}.issubset(data.columns):
        raise VendorUnavailableError(f"AKShare {endpoint} is missing required statement columns")
    data = data.copy()
    for col in ("REPORT_DATE", "NOTICE_DATE"):
        data[col] = pd.to_datetime(data[col], errors="coerce")
    data = data[(data["REPORT_DATE"] <= cutoff) & (data["NOTICE_DATE"] <= cutoff)]
    if freq == "annual":
        data = data[(data["REPORT_DATE"].dt.month == 12) & (data["REPORT_DATE"].dt.day == 31)]
    if data.empty:
        raise NoMarketDataError(ticker, symbol, f"no disclosed {freq} {title.lower()} rows through {as_of_date}")
    data = data.sort_values(["REPORT_DATE", "NOTICE_DATE"], ascending=False).drop_duplicates("REPORT_DATE")
    available = {col: label for col, label in fields.items() if col in data.columns}
    for col in available:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    calculations = calculate_statement_metrics(data, kind, available, include_quarters=freq == "quarterly")
    data = data.head(8 if freq == "quarterly" else 5)
    # Keep full disclosed history as comparison inputs, but only narrate the
    # latest period's derived metrics to bound the analyst's context cost.
    calculations = calculations[calculations["Report Period"] == data.iloc[0]["REPORT_DATE"].strftime("%Y-%m-%d")]
    if data.iloc[0][list(required)].isna().any():
        raise NoMarketDataError(ticker, symbol, f"latest {title.lower()} has missing core figures")
    columns = {"REPORT_DATE": "Report Period", "NOTICE_DATE": "Disclosure Date",
               **({"CURRENCY": "Currency"} if "CURRENCY" in data else {}),
               **({"UNIT": "Source Unit"} if "UNIT" in data else {}), **available}
    result = data[list(columns)].rename(columns=columns)
    for col in ("Report Period", "Disclosure Date"):
        result[col] = result[col].dt.strftime("%Y-%m-%d")
    period_note = (
        "Reporting dates are quarterly; income and cash flow are year-to-date cumulative, "
        "not standalone quarterly figures. Balance sheets are period-end balances."
        if freq == "quarterly" else "Annual reports only; income and cash flow cover the full year."
    )
    content = (
        f"# {title} for {ticker} ({freq})\n"
        f"# Source: AKShare / Eastmoney ({endpoint}); fetched for {as_of_date}\n"
        "# A-share statements; amounts in source currency units (yuan for CNY), except EPS per share.\n"
        f"# {period_note}\n"
        "# Current reported values, not a versioned archive of original filings.\n\n"
        + result.to_csv(index=False, float_format="%.6f")
        + "\n# Code-calculated comparisons for the latest report period; each row identifies metric, period basis, unit and inputs.\n"
        "# YoY uses the same metric, currency/unit and calendar period; prior-year base must be positive.\n"
        "# Standalone quarter = current YTD minus preceding quarter-end YTD within the same year; Q1 = YTD.\n"
        "# Balance-sheet and cash-equivalent balances are not flow differences; EPS must not be differenced.\n"
        "# Missing calculations are explicit gaps; do not substitute another metric or estimate a percentage.\n"
        "# Investing cash flow is a net flow, not capex. 购建长期资产支付现金 is not company-disclosed capex.\n"
        "# Dividends/profit distributions and interest paid cannot establish a pure dividend payout ratio.\n"
        + calculations.to_csv(index=False, float_format="%.6f")
    )
    latest = data.iloc[0]
    period = latest['REPORT_DATE'].strftime('%Y-%m-%d')
    raw_currency = latest.get('CURRENCY')
    currency = str(raw_currency).strip() if raw_currency is not None and pd.notna(raw_currency) else ''
    facts = []
    for field, label in available.items():
        is_eps = field.endswith('EPS')
        value = latest[field]
        # Preserve source units. Source amounts default to currency units, with
        # an explicit UNIT override already honored by comparison calculations.
        source_unit = latest.get('UNIT')
        unit_known = 'UNIT' not in data or (source_unit is not None and pd.notna(source_unit) and bool(str(source_unit).strip()))
        conventional = 'UNIT' not in data or (unit_known and str(source_unit).strip() in ('yuan', '元'))
        valid = pd.notna(value) and bool(currency) and unit_known and Decimal(str(value)).is_finite()
        if is_eps:
            unit = '每股货币单位'
        elif conventional and currency == 'CNY':
            unit = '百万元'
        elif unit_known and source_unit is not None:
            unit = str(source_unit).strip()
        else:
            unit = 'unknown source unit'
        amount = Decimal(str(value)) if valid else None
        if amount is not None and not is_eps and conventional and currency == 'CNY':
            amount /= Decimal('1000000')
        balance = kind == 'balance_sheet' or field in ('BEGIN_CCE', 'END_CCE')
        basis = 'period-start balance' if field == 'BEGIN_CCE' else 'period-end balance' if balance else 'annual' if freq == 'annual' else 'year-to-date cumulative'
        facts.append(Fact(key=field + '.reported', label=label, value=amount, unit=unit,
                          period=period, basis=basis, currency=currency or None,
                          status='available' if valid else 'missing',
                          qualifiers=('Current reported source values; original filing revision vintage is not verified.',)))
        for item in calculations[calculations['Metric'] == label].to_dict('records'):
            number = item['Value']
            calculated = item['Status'] == 'calculated' and pd.notna(number)
            amount = Decimal(str(number)) if calculated else None
            result_unit = '%' if 'YoY' in item['Basis'] else unit
            if amount is not None and result_unit == '百万元':
                amount /= Decimal('1000000')
            facts.append(Fact(key=field + '.' + item['Basis'].replace(' ', '_'), label=label,
                              value=amount, unit=result_unit, period=period, basis=item['Basis'],
                              currency=None if result_unit == '%' else currency or None,
                              status='available' if calculated else 'missing',
                              qualifiers=(str(item['Comparison']), str(item['Status']))))
    return SourceResult(content, tuple(facts))


def get_balance_sheet(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return get_balance_sheet_result(ticker, freq, as_of_date).content


def get_income_statement(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return get_income_statement_result(ticker, freq, as_of_date).content


def get_cashflow(ticker: str, freq: str = "quarterly", as_of_date: str | None = None) -> str:
    return get_cashflow_result(ticker, freq, as_of_date).content


def get_balance_sheet_result(ticker: str, freq: str = 'quarterly', as_of_date: str | None = None) -> SourceResult:
    return _statement_result('balance_sheet', ticker, freq, as_of_date)


def get_income_statement_result(ticker: str, freq: str = 'quarterly', as_of_date: str | None = None) -> SourceResult:
    return _statement_result('income_statement', ticker, freq, as_of_date)


def get_cashflow_result(ticker: str, freq: str = 'quarterly', as_of_date: str | None = None) -> SourceResult:
    return _statement_result('cashflow', ticker, freq, as_of_date)
