"""Verified free sources use existing identity, routing and evidence contracts."""
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents import tools
from tradingagents.agents.analysts import fundamentals_analyst, news_analyst
from tradingagents.dataflows import router
from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import VendorNotConfiguredError
from tradingagents.dataflows.facts import SourceResult
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.setup import _analyst_graph

METHODS = ('get_company_operating_history', 'get_valuation_comparison')


@pytest.mark.parametrize('name,tail', [('get_company_operating_history', ()), ('get_valuation_comparison', (3,))])
def test_source_tools_inject_run_identity_and_clamp_future_dates(monkeypatch, name, tail):
    tool = getattr(tools, name)
    schema = tool.tool_call_schema.model_json_schema()['properties']
    assert 'ticker' not in schema and 'trade_date' not in schema
    routed = Mock(return_value='Verified source')
    monkeypatch.setattr(tools, 'route_to_vendor_result', lambda *a, **kw: SourceResult(routed(*a, **kw)))
    assert tool.func('600941.SH', curr_date='2027-01-01', trade_date='2026-11-05')[0] == 'Verified source'
    routed.assert_called_once_with(name, '600941.SH', '2026-11-05', *tail)


@pytest.mark.parametrize('ticker', ['600941.SS', 'NVDA', '0941.HK'])
def test_new_sources_are_only_offered_to_a_share_fundamentals(monkeypatch, ticker):
    for module in (fundamentals_analyst, news_analyst):
        seen = []
        monkeypatch.setattr(module, 'take_turn', lambda prompt, llm, offered, messages, seen=seen:
                            (seen.extend(t.name for t in offered) or AIMessage('report'), 'report'))
        factory = module.create_fundamentals_analyst if module is fundamentals_analyst else module.create_news_analyst
        factory(None)({'company_of_interest': ticker, 'trade_date': '2026-11-05', 'messages': []})
        assert all((name in seen) is (module is fundamentals_analyst and ticker == '600941.SS') for name in METHODS)


@pytest.mark.parametrize('name,vendor', [('get_company_operating_history', 'china_mobile'), ('get_valuation_comparison', 'akshare')])
def test_optional_source_failure_returns_missing_data(monkeypatch, name, vendor):
    monkeypatch.setitem(router.VENDOR_METHODS[name], vendor, Mock(side_effect=VendorNotConfiguredError('dependency missing')))
    assert getattr(tools, name).func('600941.SS', curr_date='2026-11-05', trade_date='2026-11-05')[0].startswith('DATA_UNAVAILABLE:')


def test_fundamentals_prefetch_passes_original_source_records_downstream(monkeypatch):
    monkeypatch.setattr(tools, 'route_to_vendor', lambda method, *a, **kw: f'Original source: {method}')
    monkeypatch.setattr(tools, 'route_to_vendor_result', lambda method, *a, **kw: SourceResult(f'Original source: {method}'))
    spec = ANALYST_NODE_SPECS['fundamentals']
    def analyst(state):
        return {'messages': [AIMessage('Interpretation')], 'fundamentals_report': 'Interpretation'}
    output = _analyst_graph(spec, analyst, 1).invoke({
        'company_of_interest': '600941.SS', 'trade_date': '2026-11-05', 'messages': [],
    })
    evidence = output['fundamentals_evidence']
    assert set(METHODS).issubset({row['tool'] for row in evidence.values()})
    for name in METHODS:
        row = evidence[f'fundamentals:{name}:1']
        assert row['content'] == f'Original source: {name}'
        assert row['args']['ticker'] == '600941.SS' and row['args']['trade_date'] == '2026-11-05'


def test_valuation_obeys_configured_sources_instead_of_silently_calling_akshare(monkeypatch):
    fetch = Mock(side_effect=AssertionError('unselected source'))
    monkeypatch.setitem(router.VENDOR_METHODS['get_valuation_comparison'], 'akshare', fetch)
    with run_config({'data_vendors': {'valuation_data': 'yfinance'}}), pytest.raises(ValueError, match='Configured vendor'):
        tools.get_valuation_comparison.func('600941.SS', curr_date='2026-11-05', trade_date='2026-11-05')
    fetch.assert_not_called()
