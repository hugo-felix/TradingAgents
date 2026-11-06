"""Guard the news analyst prompt against tool-signature drift (#1116).

The prompt used to advertise ``get_news(query, ...)``, tricking the LLM into
hallucinating free-text query calls. The instrument now comes from the run's
state, so the model passes only the window.
"""
import inspect

import pytest

import tradingagents.agents.analysts.news_analyst as na
from tradingagents.agents.tools import get_news


@pytest.mark.unit
def test_get_news_takes_the_window_not_a_query():
    arg_names = set(get_news.tool_call_schema.model_json_schema()["properties"])
    assert arg_names == {"start_date", "end_date"}


@pytest.mark.unit
def test_news_prompt_matches_get_news_signature():
    src = inspect.getsource(na)
    assert "get_news(start_date, end_date)" in src
    assert "get_news(query" not in src


@pytest.mark.unit
def test_news_analyst_separates_company_evidence_background_and_inference(monkeypatch):
    from langchain_core.messages import AIMessage

    import tradingagents.agents.analysts.news_analyst as analyst

    captured = []

    def turn(prompt, *args):
        captured.append(prompt.format_messages(messages=[])[0].content)
        return AIMessage(content="report"), "report"

    monkeypatch.setattr(analyst, "take_turn", turn)
    analyst.create_news_analyst(None)({
        "trade_date": "2026-11-05", "company_of_interest": "600941.SS", "messages": [],
    })
    prompt = captured[0]
    assert "company facts" in prompt and "industry/macro background" in prompt
    assert "inference" in prompt and "DATA_UNAVAILABLE" in prompt
    assert "orders or profits" in prompt and "political" in prompt
