"""Conservative comparisons for Eastmoney's cumulative quarterly statements."""

import math

import pandas as pd


def calculate_statement_metrics(data, kind, fields, *, include_quarters=True):
    """Return auditable calculations; missing or incomparable inputs stay blank.

    Eastmoney statement amounts are in source currency units (yuan for CNY).
    An explicit UNIT column, if supplied, overrides that provider convention.
    EPS uses currency/share and cannot be recovered by subtracting YTD EPS.
    """
    data = data.copy()
    data["REPORT_DATE"] = pd.to_datetime(data["REPORT_DATE"], errors="coerce")
    rows = {row["REPORT_DATE"]: row for row in data.to_dict("records")
            if pd.notna(row["REPORT_DATE"])}
    output = []

    def number(row, field):
        try:
            value = float(row[field])
            return value if math.isfinite(value) else None
        except (KeyError, TypeError, ValueError):
            return None

    def unit(row, field):
        currency = row.get("CURRENCY")
        if pd.isna(currency) or not str(currency).strip():
            return None
        amount_unit = row.get("UNIT", "yuan" if currency == "CNY" else "source currency unit")
        if pd.isna(amount_unit) or not str(amount_unit).strip():
            return None
        return (str(currency).strip(), "share" if field.endswith("EPS") else str(amount_unit))

    def comparable(current, previous, field):
        if previous is None:
            return "not calculated: matching period missing"
        if unit(current, field) is None or unit(current, field) != unit(previous, field):
            return "not calculated: currency/unit missing or mismatched"
        if number(current, field) is None or number(previous, field) is None:
            return "not calculated: metric value missing or nonfinite"
        return None

    def quarter(date, field):
        current = rows[date]
        if (date.month, date.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
            return None, "not calculated: unsupported reporting period", ""
        if date.month == 3:
            status = comparable(current, current, field)
            return number(current, field) if status is None else None, status, date.strftime("%Y-%m-%d")
        prior_date = date - pd.offsets.QuarterEnd()
        prior = rows.get(prior_date)
        status = comparable(current, prior, field)
        comparison = f"{date:%Y-%m-%d} minus {prior_date:%Y-%m-%d} (same metric YTD)"
        return (number(current, field) - number(prior, field) if status is None else None,
                status, comparison)

    def append(date, label, basis, value, status, comparison, field):
        source_unit = unit(rows[date], field)
        output.append({"Report Period": date.strftime("%Y-%m-%d"), "Metric": label,
                       "Basis": basis, "Unit": "%" if "YoY" in basis else "/".join(source_unit or ("unknown",)),
                       "Value": value, "Comparison": comparison,
                       "Status": status or "calculated"})

    def yoy(value, previous, status):
        if status:
            return None, status
        if previous <= 0:
            return None, "not calculated: prior-year base is zero or negative"
        return (value / previous - 1) * 100, None

    for date, current in sorted(rows.items(), reverse=True):
        prior_year_date = date - pd.DateOffset(years=1)
        previous = rows.get(prior_year_date)
        for field, label in fields.items():
            stock = kind == "balance_sheet" or field in {"BEGIN_CCE", "END_CCE"}
            basis = "period-end" if stock else "YTD"
            if field == "BEGIN_CCE":
                basis = "period-start"
            status = comparable(current, previous, field)
            if (date.month, date.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
                status = "not calculated: unsupported reporting period"
            value, status = yoy(number(current, field), number(previous or {}, field), status)
            append(date, label, basis + " YoY %", value, status,
                   f"{date:%Y-%m-%d} vs {prior_year_date:%Y-%m-%d} (same metric and period)", field)
            if stock or field.endswith("EPS") or not include_quarters:
                continue
            value, status, comparison = quarter(date, field)
            append(date, label, "standalone quarter", value, status, comparison, field)
            prior_value, prior_status, prior_comparison = (
                quarter(prior_year_date, field) if previous is not None
                else (None, "not calculated: matching prior-year quarter missing", ""))
            comparison_status = comparable(current, previous, field)
            growth, growth_status = yoy(value, prior_value, status or prior_status or comparison_status)
            append(date, label, "standalone quarter YoY %", growth, growth_status,
                   f"({comparison}) vs ({prior_comparison})", field)
    return pd.DataFrame(output, columns=["Report Period", "Metric", "Basis", "Unit", "Value", "Comparison", "Status"])
