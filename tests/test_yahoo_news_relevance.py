"""Yahoo quote responses are candidates, not proof of company news coverage."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import tradingagents.dataflows.vendors.yahoo.news as news
from tradingagents.dataflows.errors import VendorUnavailableError

pytestmark = pytest.mark.unit


def article(title, tags=None, day="2026-11-04", summary=""):
    result = {"title": title, "summary": summary,
              "providerPublishTime": int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())}
    if tags is not None:
        result["relatedTickers"] = tags
    return result


def sources(monkeypatch, feed, search=()):
    calls = []
    monkeypatch.setattr(news.yf, "Ticker", lambda symbol: SimpleNamespace(get_news=lambda **kw: feed))

    def find(symbol, **kwargs):
        calls.append(symbol)
        return SimpleNamespace(news=list(search), quotes=[{"symbol": symbol}])

    monkeypatch.setattr(news.yf, "Search", find)
    return calls


def test_mainland_quote_feed_rejects_unrelated_and_summary_only_mentions(monkeypatch):
    sources(monkeypatch, [
        article("Meta issues bonds", ["META"]),
        article("SoftBank AI investment", summary="Peers include China Mobile 600941"),
        article("中国移动 (600941) releases earnings"),
    ])
    result = news.get_news_yfinance("600941.SS", "2026-11-01", "2026-11-05")
    assert "releases earnings" in result
    assert "Meta issues" not in result and "SoftBank" not in result


def test_mainland_explicit_normalized_tag_is_enough_and_future_is_excluded(monkeypatch):
    sources(monkeypatch, [
        article("Company results", ["600941.sh"]),
        {"content": {"title": "Nested company update", "pubDate": "2026-11-05T01:00:00+08:00",
                     "relatedTickers": ["600941.SS"]}},
        article("Future company results", ["600941.SS"], day="2026-11-06"),
    ])
    result = news.get_news_yfinance("600941.SH", "2026-11-01", "2026-11-05")
    assert "Company results" in result and "Nested company update" in result
    assert "Future company" not in result


def test_irrelevant_quote_feed_uses_existing_search_fallback(monkeypatch):
    calls = sources(monkeypatch, [article("Meta issues bonds", ["META"])],
                    [article("Tagged results", ["600941.SS"])])
    result = news.get_news_yfinance("600941.SS", "2026-11-01", "2026-11-05")
    assert "Tagged results" in result and calls == ["600941.SS"]


@pytest.mark.parametrize("feed", [
    [article("Meta issues bonds", ["META"])],
    [article("Future company results", ["600941.SS"], day="2026-11-06")],
    [article("Old company results", ["600941.SS"], day="2026-10-01")],
])
def test_mainland_without_valid_window_news_is_unavailable(monkeypatch, feed):
    sources(monkeypatch, feed)
    with pytest.raises(VendorUnavailableError, match="600941.SS"):
        news.get_news_yfinance("600941.SS", "2026-11-01", "2026-11-05")


def test_title_code_requires_token_boundary(monkeypatch):
    sources(monkeypatch, [article("Product 16009410 sales grow")])
    with pytest.raises(VendorUnavailableError):
        news.get_news_yfinance("600941.SS", "2026-11-01", "2026-11-05")


def test_overseas_quote_feed_keeps_untagged_but_drops_explicit_other_symbols(monkeypatch):
    calls = sources(monkeypatch, [article("Company earnings"), article("Other issuer", ["META"])])
    result = news.get_news_yfinance("AAPL", "2026-11-01", "2026-11-05")
    assert "Company earnings" in result and "Other issuer" not in result and not calls


def test_mainland_news_window_uses_shanghai_calendar_days(monkeypatch):
    sources(monkeypatch, [
        {"content": {"title": "Opening day event", "pubDate": "2026-11-04T16:01:00Z",
                     "relatedTickers": ["600941.SS"]}},
        {"content": {"title": "Next local day event", "pubDate": "2026-11-05T16:00:00Z",
                     "relatedTickers": ["600941.SS"]}},
    ])
    result = news.get_news_yfinance("600941.SS", "2026-11-05", "2026-11-05")
    assert "Opening day event" in result
    assert "Next local day event" not in result
