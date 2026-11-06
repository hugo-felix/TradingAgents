"""Financial comparisons use identical accounting fields and periods."""
import pandas as pd
import pytest

from tradingagents.dataflows.vendors.akshare import financial_metrics

FIELDS = {"PARENT_NETPROFIT": "Parent Net Profit", "NETPROFIT": "Net Profit",
          "BASIC_EPS": "Basic EPS"}


def row(date, parent=100, **extra):
    return {"REPORT_DATE": date, "CURRENCY": "CNY", "PARENT_NETPROFIT": parent,
            "NETPROFIT": parent + 10 if parent is not None else 999, "BASIC_EPS": 2, **extra}


def calculate(rows, kind="income_statement", fields=FIELDS):
    return financial_metrics.calculate_statement_metrics(pd.DataFrame(rows), kind, fields)


def result(table, metric="Parent Net Profit", basis="standalone quarter", date="2026-09-30"):
    return table[(table["Report Period"] == date) & (table["Metric"] == metric)
                 & (table["Basis"] == basis)].iloc[0]


def test_parent_profit_quarter_and_yoy_never_mix_total_profit():
    table = calculate([row("2026-09-30", 106198000000, NETPROFIT=120000000000),
                       row("2026-06-30", 78934000000, NETPROFIT=85000000000),
                       row("2025-09-30", 100000000000), row("2025-06-30", 75000000000)])
    assert result(table)["Value"] == 27264000000
    assert result(table, basis="YTD YoY %")["Value"] == pytest.approx(6.198)
    assert result(table, basis="standalone quarter YoY %")["Value"] == pytest.approx(9.056)
    assert "2026-06-30" in result(table)["Comparison"]


@pytest.mark.parametrize("extra", [{"CURRENCY": "USD"}, {"UNIT": "million"}])
def test_currency_and_unit_mismatch_prevent_comparisons(extra):
    table = calculate([row("2026-09-30", UNIT="yuan"),
                       row("2026-06-30", UNIT="yuan", **({} if "UNIT" in extra else extra))
                       | extra,
                       row("2025-09-30", UNIT="yuan") | extra])
    assert pd.isna(result(table)["Value"])
    assert pd.isna(result(table, basis="YTD YoY %")["Value"])
    assert "currency/unit" in result(table)["Status"]


@pytest.mark.parametrize("base", [0, -100, None])
def test_zero_negative_or_missing_base_does_not_produce_yoy(base):
    table = calculate([row("2026-09-30"), row("2025-09-30", base)])
    assert pd.isna(result(table, basis="YTD YoY %")["Value"])
    assert result(table, basis="YTD YoY %")["Status"] != "calculated"


def test_missing_previous_quarter_same_day_or_currency_is_explicit_gap():
    table = calculate([row("2026-09-30"), row("2026-03-31"), row("2025-09-29")])
    assert pd.isna(result(table)["Value"])
    assert pd.isna(result(table, basis="YTD YoY %")["Value"])
    table = calculate([row("2026-09-30", CURRENCY=None), row("2026-06-30")])
    assert pd.isna(result(table)["Value"])


def test_balance_eps_and_cash_equivalents_are_not_differenced():
    balance = calculate([row("2026-09-30"), row("2026-06-30"), row("2025-09-30", 80)],
                        "balance_sheet", {"PARENT_NETPROFIT": "Balance"})
    assert set(balance["Basis"]) == {"period-end YoY %"}
    assert result(balance, "Balance", "period-end YoY %")["Value"] == 25
    income = calculate([row("2026-09-30"), row("2026-06-30")])
    assert not ((income["Metric"] == "Basic EPS") & income["Basis"].str.startswith("standalone")).any()
    cash = calculate([row("2026-09-30", END_CCE=90), row("2026-06-30", END_CCE=70)],
                     "cashflow", {"END_CCE": "Ending Cash Equivalents"})
    assert set(cash["Basis"]) == {"period-end YoY %"}


def test_first_quarter_is_already_standalone_and_missing_metric_does_not_substitute():
    table = calculate([row("2026-03-31", 25)])
    assert result(table, date="2026-03-31")["Value"] == 25
    table = calculate([row("2026-09-30"), row("2026-06-30", None)])
    assert pd.isna(result(table)["Value"])


def test_beginning_cash_is_labeled_as_a_period_start_balance():
    table = calculate([row("2026-09-30", BEGIN_CCE=100), row("2025-09-30", BEGIN_CCE=80)],
                      "cashflow", {"BEGIN_CCE": "Beginning Cash Equivalents"})
    assert set(table["Basis"]) == {"period-start YoY %"}


@pytest.mark.parametrize("metadata", [{"CURRENCY": pd.NA}, {"UNIT": ""}])
def test_missing_currency_or_explicit_empty_unit_is_a_gap(metadata):
    table = calculate([row("2026-09-30", **metadata), row("2026-06-30", **metadata)])
    assert pd.isna(result(table)["Value"])
