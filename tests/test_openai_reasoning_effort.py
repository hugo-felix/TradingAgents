"""OpenAI ``reasoning_effort`` is gated to reasoning models.

Non-reasoning OpenAI models (gpt-4.1, gpt-4o, ...) 400 with "Unsupported
parameter: 'reasoning.effort'". The client must drop the kwarg for those rather
than forward it and crash the run. The GPT-5 family and the o-series accept it.
"""

import pytest

from tradingagents.llm_clients.openai_client import (
    OpenAIClient,
    _supports_reasoning_effort,
)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-5.5", True), ("gpt-5.4", True), ("gpt-5.4-mini", True),
        ("gpt-5.5-pro", True), ("gpt-6-astra", True), ("o1", True), ("o3-mini", True),
        ("gpt-4.1", False), ("gpt-4o", False), ("gpt-4o-mini", False),
        ("gpt-3.5-turbo", False), ("gpt-10", True),
        ("gpt-5foo", False), ("gpt-60x", False), ("o3rd-party", False),
    ],
)
def test_supports_reasoning_effort(model, expected):
    assert _supports_reasoning_effort(model) is expected


def _effort_on(model, monkeypatch):
    # A fake key lets get_llm() construct the client without a network call.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm = OpenAIClient(model, provider="openai", reasoning_effort="low").get_llm()
    return getattr(llm, "reasoning_effort", None)


def test_reasoning_model_receives_effort(monkeypatch):
    assert _effort_on("gpt-5.4-mini", monkeypatch) == "low"


def test_non_reasoning_model_drops_effort(monkeypatch):
    # gpt-4.1 would 400 with reasoning_effort — it must be dropped.
    assert _effort_on("gpt-4.1", monkeypatch) is None


@pytest.mark.parametrize("effort", ["none", "low"])
def test_local_endpoint_receives_explicit_effort(effort):
    from tradingagents.llm_clients.factory import create_tier_client

    llm = create_tier_client({
        "llm_provider": "openai_compatible",
        "backend_url": "http://localhost:8888/v1",
        "quick_think_llm": "Qwen3.8-27B-GGUF:UD-Q4_K_XL",
        "openai_reasoning_effort": effort,
        "max_tokens": 8192,
    }, "quick").get_llm()
    payload = llm._get_request_payload("test")
    assert payload["reasoning_effort"] == effort
    assert payload.get("max_completion_tokens", payload.get("max_tokens")) == 8192


def test_local_endpoint_does_not_default_to_reasoning():
    llm = OpenAIClient("custom-local-model", provider="openai_compatible",
                       base_url="http://localhost:8888/v1").get_llm()
    assert "reasoning_effort" not in llm._get_request_payload("test")
