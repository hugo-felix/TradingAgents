"""Offline evidence acquisition and propagation through the real ToolNode."""
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from tradingagents.agents import tools
from tradingagents.agents.state import AgentState
from tradingagents.dataflows.facts import SourceResult
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.setup import _analyst_graph

DATE = '2026-11-05'

@pytest.fixture(autouse=True)
def mocked_source_artifacts(monkeypatch):
    # Wiring tests mock the text router; adapt that fixture to the artifact channel.
    monkeypatch.setattr(tools, 'route_to_vendor_result',
                        lambda *a, **kw: SourceResult(tools.route_to_vendor(*a, **kw)))



def _state(ticker='600941.SH'):
    return {'company_of_interest': ticker, 'trade_date': DATE, 'messages': [HumanMessage('analyze')]}


@pytest.mark.unit
def test_collect_only_tool_sources_and_keep_call_args():
    from tradingagents.agents.evidence import collect_tool_evidence, render_source_evidence
    messages = [AIMessage('invented profit 999', tool_calls=[{'id': 'x', 'name': 'get_cashflow', 'args': {'freq': 'quarterly'}}]),
                ToolMessage('Operating cashflow: 123 CNY', tool_call_id='x', name='get_cashflow')]
    evidence = collect_tool_evidence('fundamentals', messages)
    assert evidence == {'fundamentals:get_cashflow:1': {'tool': 'get_cashflow', 'args': {'freq': 'quarterly'}, 'status': 'available', 'content': 'Operating cashflow: 123 CNY'}}
    rendered = render_source_evidence({'fundamentals_evidence': evidence})
    assert '123 CNY' in rendered and 'fundamentals:get_cashflow:1' in rendered
    assert '999' not in rendered


@pytest.mark.unit
@pytest.mark.parametrize('content,status', [('', 'empty'), ('DATA_UNAVAILABLE: disabled', 'unavailable'), ('Statements are withheld for this historical date', 'withheld'), ('Error: upstream failed', 'error'),
    ('# AKShare dividend schemes\nDividend coverage is insufficient for disclosed schemes through this date; do not assume zero dividends.', 'unavailable'),
    ('# China macro\n# Macro coverage insufficient: no usable rows in the requested period window.', 'unavailable')])
def test_missing_sources_are_never_available(content, status):
    from tradingagents.agents.evidence import collect_tool_evidence, evidence_unavailable
    evidence = collect_tool_evidence('market', [ToolMessage(content, tool_call_id='x', name='get_stock_data')])
    assert next(iter(evidence.values()))['status'] == status
    assert evidence_unavailable({**_state(), 'market_evidence': evidence})
    assert not evidence_unavailable(_state())
    assert not evidence_unavailable({**_state('NVDA'), 'market_evidence': evidence})


@pytest.mark.unit
@pytest.mark.parametrize('role,names', [('market', {'get_stock_data', 'get_verified_market_snapshot'}), ('fundamentals', {'get_fundamentals', 'get_balance_sheet', 'get_cashflow', 'get_income_statement', 'get_company_disclosures', 'get_dividends', 'get_company_operating_history', 'get_valuation_comparison'}), ('news', {'get_news', 'get_company_disclosures'})])
def test_early_final_still_prefetches_required_sources_with_injected_identity(role, names):
    seen = []
    spec = ANALYST_NODE_SPECS[role]
    def agent(state):
        seen.extend(state['messages'])
        return {'messages': [AIMessage('My interpretation')], spec.report_key: 'My interpretation'}
    with patch('tradingagents.agents.tools.route_to_vendor', return_value='source data') as routed, patch('tradingagents.agents.tools.build_verified_market_snapshot_result', return_value=SourceResult('verified close 100')) as snapshot:
        out = _analyst_graph(spec, agent, 1).invoke(_state())
    evidence = out[f'{role}_evidence']
    assert {v['tool'] for v in evidence.values()} == names
    assert all(v['args']['trade_date'] == DATE for v in evidence.values())
    assert all((v['args'].get('ticker') or v['args'].get('symbol')) == '600941.SH' for v in evidence.values())
    assert all(c.args[1] == '600941.SH' for c in routed.call_args_list)
    if role == 'market':
        snapshot.assert_called_once_with('600941.SH', DATE, 30)
        assert routed.call_args.args == ('get_stock_data', '600941.SH', '2025-10-01', DATE)
    assert any(isinstance(m, ToolMessage) for m in seen)
    assert 'source coverage' in out[spec.report_key].lower()
    assert 'My interpretation' not in str(evidence)


@pytest.mark.unit
def test_prefetch_does_not_spend_model_round_or_repeat():
    states = []
    spec = ANALYST_NODE_SPECS['news']
    def agent(state):
        states.append(state)
        if len(states) < 3:
            return {'messages': [AIMessage('', tool_calls=[{'name': 'get_news', 'args': {'start_date': DATE, 'end_date': DATE}, 'id': f'm{len(states)}'}])], spec.report_key: ''}
        return {'messages': [AIMessage('final')], spec.report_key: 'final'}
    with patch('tradingagents.agents.tools.route_to_vendor', return_value='source') as routed:
        out = _analyst_graph(spec, agent, 2).invoke(_state())
    assert len(states) == 3
    assert sum(c.args[0] == 'get_company_disclosures' for c in routed.call_args_list) == 1
    assert len(out['news_evidence']) == 4


@pytest.mark.unit
def test_failed_tool_is_recorded_without_stopping_other_sources():
    spec = ANALYST_NODE_SPECS['news']
    def agent(state):
        return {'messages': [AIMessage('final')], spec.report_key: 'final'}
    with patch('tradingagents.agents.tools.route_to_vendor', side_effect=RuntimeError('upstream unavailable')):
        out = _analyst_graph(spec, agent, 1).invoke(_state())
    assert {v['status'] for v in out['news_evidence'].values()} == {'error'}


@pytest.mark.unit
def test_non_a_share_does_not_prefetch():
    spec = ANALYST_NODE_SPECS['news']
    with patch('tradingagents.agents.tools.route_to_vendor') as routed:
        out = _analyst_graph(spec, lambda s: {'messages': [AIMessage('final')], spec.report_key: 'final'}, 1).invoke(_state('NVDA'))
    routed.assert_not_called()
    assert out[spec.report_key] == 'final'


@pytest.mark.unit
def test_parallel_roles_keep_independent_source_fields():
    graph = StateGraph(AgentState)
    for role in ('market', 'news', 'fundamentals'):
        spec = ANALYST_NODE_SPECS[role]
        graph.add_node(role, _analyst_graph(spec, lambda s, key=spec.report_key: {'messages': [AIMessage('final')], key: 'final'}, 1))
        graph.add_edge(START, role)
        graph.add_edge(role, END)
    with patch('tradingagents.agents.tools.route_to_vendor', return_value='source'), patch('tradingagents.agents.tools.build_verified_market_snapshot_result', return_value=SourceResult('snapshot')):
        out = graph.compile().invoke(_state())
    assert all(out[f'{role}_evidence'] for role in ('market', 'news', 'fundamentals'))


@pytest.mark.unit
def test_source_content_is_bounded_and_truncation_is_visible():
    from tradingagents.agents.evidence import MAX_ROLE_CONTENT, collect_tool_evidence
    messages = [ToolMessage('comparison table\n' * 4000, tool_call_id=str(i), name='get_cashflow') for i in range(10)]
    evidence = collect_tool_evidence('fundamentals', messages)
    assert len(evidence) == 10
    assert sum(len(row['content']) for row in evidence.values()) <= MAX_ROLE_CONTENT
    assert all('TRUNCATED' in row['content'] for row in evidence.values())


@pytest.mark.unit
def test_partial_source_does_not_mean_every_fact_is_verified():
    from tradingagents.agents.evidence import collect_tool_evidence, evidence_unavailable
    source = 'Verified close 100\nRSI unavailable: insufficient history'
    evidence = collect_tool_evidence('market', [ToolMessage(source, tool_call_id='a', name='get_verified_market_snapshot')])
    assert next(iter(evidence.values()))['status'] == 'available'
    assert not evidence_unavailable({**_state(), 'market_evidence': evidence, 'news_evidence': {}})


@pytest.mark.unit
def test_disclosure_28k_and_small_sources_are_kept_in_full():
    from tradingagents.agents.evidence import collect_tool_evidence
    disclosure = 'CNInfo original announcement\n' + '中' * 27_900
    small_sources = ['small original statement\n' + '数' * 1000 for _ in range(5)]
    messages = [ToolMessage(disclosure, tool_call_id='disclosure', name='get_company_disclosures')]
    messages += [ToolMessage(content, tool_call_id=str(i), name='get_cashflow') for i, content in enumerate(small_sources)]
    evidence = collect_tool_evidence('fundamentals', messages)
    assert evidence['fundamentals:get_company_disclosures:1']['content'] == disclosure
    assert [row['content'] for row in evidence.values()][1:] == small_sources


@pytest.mark.unit
def test_over_budget_reuses_small_sources_unused_shares():
    from tradingagents.agents.evidence import MAX_ROLE_CONTENT, collect_tool_evidence
    originals = ['small' * 200, *['announcement' * 3000 for _ in range(3)]]
    messages = [ToolMessage(content, tool_call_id=str(i), name='get_company_disclosures') for i, content in enumerate(originals)]
    contents = [row['content'] for row in collect_tool_evidence('news', messages).values()]
    assert contents[0] == originals[0]
    assert all(len(content) > MAX_ROLE_CONTENT // len(messages) for content in contents[1:])
    assert sum(map(len, contents)) == MAX_ROLE_CONTENT
    assert all('TRUNCATED' in content for content in contents[1:])
