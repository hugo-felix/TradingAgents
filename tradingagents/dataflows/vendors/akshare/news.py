"""Recent company news with resolved-name search and listing-code fallback."""

import logging
import re

import pandas as pd

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorError, VendorUnavailableError

from . import common

logger = logging.getLogger(__name__)

def _matching_news(data: pd.DataFrame, code: str, name: str | None, start, end) -> list:
    if data.empty:
        return []
    if not {"新闻标题", "新闻内容", "发布时间"}.issubset(data.columns):
        raise VendorUnavailableError("AKShare stock_news_em is missing required news columns")
    articles = []
    for row in data.to_dict("records"):
        title = row.get("新闻标题")
        if not isinstance(title, str) or not title.strip():
            continue
        if not (re.search(rf"(?<!\d){code}(?!\d)", title) or (name and name in title)):
            continue
        try:
            when = pd.Timestamp(row["发布时间"])
            if pd.isna(when):
                continue
            when = when.tz_localize("Asia/Shanghai") if when.tzinfo is None else when.tz_convert("Asia/Shanghai")
        except (ValueError, TypeError):
            continue
        if start <= when < end:
            articles.append((when, row))
    return articles


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    code, _ = common.resolve_symbol(ticker)
    start = pd.Timestamp(start_date, tz="Asia/Shanghai")
    end = pd.Timestamp(end_date, tz="Asia/Shanghai") + pd.Timedelta(days=1)
    if start >= end:
        raise ValueError("start_date must not be after end_date")
    # Use the provider's stock identity rather than guessing a name from the
    # listing code. Search by that name first; the code remains usable when
    # identity resolution or the name search is unavailable.
    name = None
    try:
        profile = common.request("stock_individual_info_em", symbol=code)
        if {"item", "value"}.issubset(profile.columns):
            names = profile.loc[profile["item"] == "股票简称", "value"].dropna()
            if not names.empty:
                candidate = str(names.iloc[0]).strip()
                if candidate.lower() not in ("", "-", "none", "nan", "null", "n/a"):
                    name = candidate
    except VendorError:
        pass
    queries = [(code, "listing code; company name unavailable")]
    if name and name != code:
        queries = [(name, "company name first"), (code, "listing-code fallback")]
    articles = []
    for term, method in queries:
        try:
            data = common.request("stock_news_em", symbol=term)
            articles = _matching_news(data, code, name, start, end)
        except VendorUnavailableError as exc:
            if term == code:
                raise
            logger.warning("AKShare name search unavailable for %s; trying listing code: %s", ticker, exc)
            continue
        if articles:
            query = f"{term} ({method})"
            break
    if not articles:
        raise VendorUnavailableError(
            f"AKShare/Eastmoney news unavailable for {ticker} in {start_date}..{end_date}: "
            "no company-specific dated headlines in the recent search samples; "
            "this is not an absence of company news or events"
        )
    articles.sort(key=lambda article: article[0], reverse=True)
    unique = []
    seen_urls, seen_titles = set(), set()
    for when, row in articles:
        url = row.get("新闻链接")
        url = url.strip() if isinstance(url, str) else ""
        # Same-day title duplicates are likely reprints; equal headlines on
        # different days can describe separate events and must not be merged.
        title_key = (when.date(), re.sub(r"\s+", "", row["新闻标题"]).casefold())
        if (url and url in seen_urls) or title_key in seen_titles:
            continue
        if url:
            seen_urls.add(url)
        seen_titles.add(title_key)
        unique.append((when, row))
    lines = [f"## {ticker} News from {start_date} to {end_date}",
             "Source: AKShare / Eastmoney; publication times in Asia/Shanghai.",
             f"Search query: {query}.",
             "Recent search sample only, not a complete historical archive. Headlines mention the company; verify the article before treating it as a company disclosure.",
             "Company-name matches may concern the parent group, subsidiaries or H-shares. Verify issuer and share class before attributing financial impact to this A-share listing.",
             "URL and same-day title duplicates are removed; similar articles can still describe one event. Article counts are not counts of independent events.", ""]
    for when, row in unique[:get_config()["news_article_limit"]]:
        lines += [f"### {row['新闻标题']}", f"Published: {when.isoformat()}", str(row['新闻内容'])]
        for col in ("文章来源", "新闻链接"):
            value = row.get(col)
            if isinstance(value, str) and value:
                lines.append(value)
        lines.append("")
    return "\n".join(lines)
