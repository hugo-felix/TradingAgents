"""Avoid repeated source requests and prompt text without discarding audit records."""
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tradingagents.agents import tools
from tradingagents.agents.analysts import fundamentals_analyst, market_analyst, news_analyst
from tradingagents.agents.context import get_instrument_context_from_state
from tradingagents.agents.evidence import render_source_evidence
from tradingagents.dataflows.facts import SourceResult
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup, _analyst_graph

DATE = '2026-11-05'
MARKER = 'UNIQUE_ORIGINAL_SOURCE_TEXT'

@pytest.fixture(autouse=True)
def mocked_source_artifacts(monkeypatch):
    # Wiring tests mock the text router; adapt that fixture to the artifact channel.
    monkeypatch.setattr(tools, 'route_to_vendor_result',
                        lambda *a, **kw: SourceResult(tools.route_to_vendor(*a, **kw)))



def state():
    return {'company_of_interest': '600941.SH', 'trade_date': DATE,
            'messages': [HumanMessage('analyze')]}


@pytest.mark.parametrize('role,module,source,extra', [
    ('market', market_analyst, 'get_stock_data', 'get_verified_market_snapshot'),
    ('fundamentals', fundamentals_analyst, 'get_cashflow', 'get_dividends'),
    ('news', news_analyst, 'get_news', 'get_global_news'),
])
def test_each_analyst_sees_one_copy_per_original_tool_result(role, module, source, extra):
    captured = []

    def turn(prompt, llm, tools, messages):
        captured.append('\n'.join(str(m.content) for m in prompt.format_messages(messages=messages)))
        if len(captured) == 1:
            return AIMessage('', tool_calls=[{'name': extra, 'args': {'curr_date': DATE}, 'id': 'extra'}]), ''
        return AIMessage('report'), 'report'

    factory = getattr(module, f'create_{role}_analyst')
    with patch.object(module, 'take_turn', side_effect=turn), patch(
        'tradingagents.agents.tools.route_to_vendor',
        side_effect=lambda method, *a, **k: MARKER if method == source else 'Other source',
    ), patch('tradingagents.agents.tools.build_verified_market_snapshot_result', return_value=SourceResult('Snapshot')):
        result = _analyst_graph(ANALYST_NODE_SPECS[role], factory(None), 2).invoke(state())
    assert [text.count(MARKER) for text in captured] == [1, 1]
    assert MARKER in get_instrument_context_from_state({**state(), **result})
    assert 'Prefer results already supplied' in captured[0]


@pytest.mark.parametrize('selected,owner', [
    (('news', 'fundamentals'), 'fundamentals'),
    (('fundamentals', 'news'), 'fundamentals'),
    (('news',), 'news'), (('fundamentals',), 'fundamentals'),
])
def test_selected_analysts_prefetch_disclosures_once_and_keep_the_original(selected, owner):
    def report(role):
        return lambda s: {'messages': [AIMessage('report')], f'{role}_report': 'report'}

    with patch('tradingagents.graph.setup.create_news_analyst', return_value=report('news')), patch(
        'tradingagents.graph.setup.create_fundamentals_analyst', return_value=report('fundamentals'),
    ), patch('tradingagents.agents.tools.route_to_vendor', return_value=MARKER) as routed:
        workflow = GraphSetup(None, None, ConditionalLogic(), 1).setup_graph(selected)
        nodes = [ANALYST_NODE_SPECS[role].agent_node for role in selected]
        result = workflow.compile(interrupt_after=nodes).invoke(state())
    calls = [c for c in routed.call_args_list if c.args[0] == 'get_company_disclosures']
    assert len(calls) == 1
    assert any(row['tool'] == 'get_company_disclosures' for row in result[f'{owner}_evidence'].values())
    if owner == 'fundamentals' and 'news' in selected:
        assert all(row['tool'] != 'get_company_disclosures' for row in result['news_evidence'].values())
    assert MARKER in get_instrument_context_from_state(result)


def test_identical_results_render_once_with_all_ids_and_without_mutating_state():
    row = {'tool': 'get_company_disclosures', 'args': {'ticker': '600941.SH', 'curr_date': DATE},
           'status': 'available', 'content': MARKER}
    current = {'fundamentals_evidence': {'fundamentals:disclosures:1': row},
               'news_evidence': {'news:disclosures:1': {**row, 'args': dict(reversed(list(row['args'].items())))}}}
    rendered = render_source_evidence(current)
    assert rendered.count(MARKER) == 1
    assert 'fundamentals:disclosures:1' in rendered and 'news:disclosures:1' in rendered
    assert current['news_evidence']['news:disclosures:1']['content'] == MARKER


@pytest.mark.parametrize('change', [
    {'args': {'ticker': '600941.SH', 'curr_date': '2026-11-04'}},
    {'content': 'Different original version'}, {'status': 'unavailable'}, {'tool': 'different_tool'},
])
def test_different_source_results_remain_separate(change):
    row = {'tool': 'get_company_disclosures', 'args': {}, 'status': 'available', 'content': MARKER}
    rendered = render_source_evidence({'news_evidence': {'source:1': row, 'source:2': {**row, **change}}})
    assert rendered.count('\nSource source:') == 2
