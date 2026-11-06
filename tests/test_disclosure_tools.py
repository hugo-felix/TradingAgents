"""New disclosure/dividend tools carry the run identity and date to vendors."""
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents import tools
from tradingagents.agents.analysts import fundamentals_analyst, news_analyst
from tradingagents.dataflows.facts import SourceResult


@pytest.mark.parametrize('name', ['get_company_disclosures', 'get_dividends'])
def test_new_tool_identity_and_trade_date_are_hidden(name):
    tool = getattr(tools, name)
    fields = tool.tool_call_schema.model_json_schema()['properties']
    assert 'ticker' not in fields and 'trade_date' not in fields


@pytest.mark.parametrize('name,tail', [('get_company_disclosures', (730, 6)), ('get_dividends', ())])
def test_new_tool_uses_run_date_when_model_asks_for_future(monkeypatch, name, tail):
    routed = Mock(return_value='source data')
    monkeypatch.setattr(tools, 'route_to_vendor_result', lambda *a, **kw: SourceResult(routed(*a, **kw)))
    out, artifact = getattr(tools, name).func('600941.SS', curr_date='2027-01-01', trade_date='2026-11-05')
    assert out == 'source data'
    routed.assert_called_once_with(name, '600941.SS', '2026-11-05', *tail)


@pytest.mark.parametrize('module', [fundamentals_analyst, news_analyst])
@pytest.mark.parametrize('ticker,enabled', [('600941.SS', True), ('NVDA', False)])
def test_disclosure_tools_are_only_offered_for_a_shares(monkeypatch, module, ticker, enabled):
    seen = []

    def turn(prompt, llm, offered, messages):
        seen.extend(t.name for t in offered)
        return AIMessage(content='report'), 'report'

    monkeypatch.setattr(module, 'take_turn', turn)
    create = module.create_fundamentals_analyst if module is fundamentals_analyst else module.create_news_analyst
    create(None)({'company_of_interest': ticker, 'trade_date': '2026-11-05', 'messages': []})
    assert ('get_company_disclosures' in seen) is enabled
    if module is fundamentals_analyst:
        assert ('get_dividends' in seen) is enabled


@pytest.mark.parametrize('method,vendor', [('get_company_disclosures', 'cninfo'), ('get_dividends', 'akshare')])
def test_optional_disclosure_dependencies_do_not_abort_analysis(monkeypatch, method, vendor):
    from tradingagents.dataflows import router
    from tradingagents.dataflows.errors import VendorNotConfiguredError

    missing = Mock(side_effect=VendorNotConfiguredError('optional dependency missing'))
    monkeypatch.setitem(router.VENDOR_METHODS[method], vendor, missing)
    out, artifact = getattr(tools, method).func('600941.SS', curr_date='2026-11-05', trade_date='2026-11-05')
    assert out.startswith('DATA_UNAVAILABLE:') and 'optional' in out
