"""GLM wire contracts exercised locally through the real LangChain/OpenAI stack."""
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from tradingagents.default_config import build_default_config
from tradingagents.llm_clients.factory import (
    build_llm_kwargs,
    create_llm_client,
    create_tier_client,
)


class Decision(BaseModel):
    action: str = Field(description="Trading action backed by the supplied evidence")
    confidence: float


def make_llm(monkeypatch, model="glm-5.3-flash", provider="glm-cn", **kwargs):
    monkeypatch.setenv("ZHIPU_CN_API_KEY", "local-test-key")
    monkeypatch.setenv("ZHIPU_API_KEY", "local-test-key")
    return create_llm_client(provider, model, **kwargs).get_llm()


@pytest.mark.parametrize("provider", ["glm", "glm-cn"])
def test_config_routes_glm_effort_independently(provider, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_GLM_REASONING_EFFORT", "low")
    config = {**build_default_config(), "llm_provider": provider, "openai_reasoning_effort": "high"}
    assert build_llm_kwargs(config)["reasoning_effort"] == "low"
    config["llm_provider"] = "openai"
    assert build_llm_kwargs(config)["reasoning_effort"] == "high"


@pytest.mark.parametrize("model,effort", [("glm-5.2", "medium"), ("glm-5.3", "low"), ("glm-5.3-flash", "max")])
def test_glm_reasoning_and_token_payload(model, effort, monkeypatch):
    llm = make_llm(monkeypatch, model, reasoning_effort=effort, max_tokens=6000)
    payload = llm._get_request_payload([HumanMessage(content="Analyze supplied evidence")])
    assert payload["reasoning_effort"] == effort
    assert payload["max_tokens"] == 6000
    assert "max_completion_tokens" not in payload
    assert "thinking" not in payload


@pytest.mark.parametrize("model,effort", [("glm-4.5-air", "low"), ("glm-4.7", "low"), ("glm-5.1", "low"), ("glm-5.3-flash", "medium"), ("glm-5.3", "none"), ("glm-5.2", "typo")])
def test_bad_glm_effort_fails_at_client_creation(model, effort, monkeypatch):
    with pytest.raises(ValueError, match="reasoning_effort"):
        make_llm(monkeypatch, model, reasoning_effort=effort)


@pytest.mark.parametrize("model", ["glm-4.5-air", "glm-4.7"])
def test_older_glm_does_not_receive_effort_by_default(model, monkeypatch):
    assert "reasoning_effort" not in make_llm(monkeypatch, model)._get_request_payload("hello")


@pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash"])
def test_glm53_defaults_to_bounded_thinking_and_preserves_explicit_effort(model, monkeypatch):
    assert make_llm(monkeypatch, model)._get_request_payload("hello")["reasoning_effort"] == "low"
    assert make_llm(monkeypatch, model, reasoning_effort="max")._get_request_payload("hello")["reasoning_effort"] == "max"


@pytest.mark.parametrize("prompt", ["Analyze supplied evidence", [HumanMessage(content="Analyze supplied evidence")], ChatPromptTemplate.from_messages([("user", "Analyze supplied evidence")]).invoke({})])
def test_glm_json_output_sends_schema_and_parses_mocked_response(prompt, monkeypatch):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 0, "model": "glm-5.3-flash", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"action":"HOLD","confidence":0.7}'}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        llm = make_llm(monkeypatch, reasoning_effort="low", max_tokens=6000, http_client=http_client)
        result = llm.with_structured_output(Decision).invoke(prompt)
    assert result == Decision(action="HOLD", confidence=0.7)
    payload = requests[0]
    assert payload["response_format"] == {"type": "json_object"}
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert payload["reasoning_effort"] == "low"
    assert payload["max_tokens"] == 6000
    assert "max_completion_tokens" not in payload
    instruction = payload["messages"][0]
    assert instruction["role"] == "system"
    assert "JSON" in instruction["content"]
    assert '"required": ["action", "confidence"]' in instruction["content"]
    assert Decision.model_fields["action"].description in instruction["content"]
    assert payload["messages"][-1]["content"] == "Analyze supplied evidence"


def test_mixed_tier_glm_receives_own_reasoning_setting(monkeypatch):
    monkeypatch.setenv("ZHIPU_CN_API_KEY", "local-test-key")
    monkeypatch.setenv("ZHIPU_API_KEY", "local-test-key")
    config = {"llm_provider": "openai", "quick_think_provider": "glm-cn", "quick_think_llm": "glm-5.3-flash", "glm_reasoning_effort": "low", "openai_reasoning_effort": "high"}
    llm = create_tier_client(config, "quick").get_llm()
    assert llm._get_request_payload("hello")["reasoning_effort"] == "low"


def test_glm_json_mode_binding_does_not_hide_invalid_parameters(monkeypatch):
    llm = make_llm(monkeypatch)
    with pytest.raises(ValueError, match="strict"):
        llm.with_structured_output(Decision, strict=True)


def test_glm_explicit_function_calling_does_not_force_unsupported_tool_choice(monkeypatch):
    llm = make_llm(monkeypatch)
    bound = llm.with_structured_output(Decision, method="function_calling")
    payload = llm._get_request_payload("hello", **bound.first.kwargs)
    assert "tools" in payload
    assert "tool_choice" not in payload
