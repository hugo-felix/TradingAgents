"""Avoid repeated source requests and prompt text without discarding audit records."""
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableLambda

from tradingagents.agents import tools
from tradingagents.agents.analysts import fundamentals_analyst, market_analyst, news_analyst
from tradingagents.agents.analysts.turn import WRAP_UP, take_turn
from tradingagents.agents.context import get_instrument_context_from_state
from tradingagents.agents.evidence import collect_tool_evidence, render_source_evidence
from tradingagents.dataflows.facts import SourceResult
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup, _analyst_graph

DATE = '2026-11-05'
MARKER = 'UNIQUE_ORIGINAL_SOURCE_TEXT'


def test_duplicate_evidence_keeps_first_fact_source_and_original_ids():
    messages = []
    artifact = {'fact_schema_version': 1, 'facts': [{'key': 'revenue', 'value': 10}]}
    for index, (args, content, facts, status) in enumerate([
        ({'ticker': '600941', 'date': DATE}, MARKER, artifact, 'success'),
        ({'date': DATE, 'ticker': '600941'}, MARKER, artifact, 'success'),
        ({'ticker': '600941', 'date': DATE}, 'Changed', artifact, 'success'),
        ({'ticker': '600941', 'date': '2025-01-01'}, MARKER, artifact, 'success'),
        ({'ticker': '600941', 'date': DATE}, MARKER, None, 'success'),
        ({'ticker': '600941', 'date': DATE}, MARKER, artifact, 'error'),
    ]):
        messages.append(ToolMessage(content, name='source', tool_call_id=str(index),
                                    additional_kwargs={'source_args': args},
                                    artifact=facts, status=status))
    evidence = collect_tool_evidence('fundamentals', messages)
    assert list(evidence) == [f'fundamentals:source:{i}' for i in (1, 3, 4, 5, 6)]
    assert evidence['fundamentals:source:1']['facts'] == artifact['facts']
    assert len(messages) == 6 and messages[1].content == MARKER


@pytest.mark.parametrize('wrap_up', [False, True])
def test_repeated_tool_payload_reaches_model_once_without_losing_history(wrap_up):
    messages = [HumanMessage('analyze')]
    for index, content in enumerate([MARKER, MARKER, 'Changed source']):
        messages.extend([
            AIMessage('', tool_calls=[{'name': 'source', 'args': {}, 'id': str(index)}]),
            ToolMessage(content, name='source', tool_call_id=str(index)),
        ])
    if wrap_up:
        messages.append(HumanMessage(WRAP_UP))
    captured = []
    responder = RunnableLambda(lambda p: captured.append(p.to_messages()) or AIMessage('report'))

    class Model:
        def bind_tools(self, tools):
            return responder

    prompt = ChatPromptTemplate.from_messages([MessagesPlaceholder('messages')])
    take_turn(prompt, responder if wrap_up else Model(), [], messages)
    text = '\n'.join(str(m.content) for m in captured[0])
    assert text.count(MARKER) == 1
    assert 'Changed source' in text
    assert messages[4].content == MARKER
    assert len(captured[0]) == len(messages)
    if not wrap_up:
        assert [m.tool_call_id for m in captured[0] if isinstance(m, ToolMessage)] == ['0', '1', '2']


@pytest.mark.parametrize('wrap_up', [True, False])
def test_grounded_report_uses_schema_mode_after_tool_selection(wrap_up):
    from types import SimpleNamespace

    from langchain_core.runnables import RunnableLambda

    from tradingagents.agents.grounding import GroundedReport

    captured = []

    class Model:
        def with_structured_output(self, schema, **kwargs):
            assert schema is GroundedReport and kwargs['include_raw']
            return SimpleNamespace(invoke=lambda messages: (
                captured.extend(messages) or {'raw': AIMessage('{"summary":"qualitative"}')}
            ))

        def bind_tools(self, tools):
            assert not wrap_up, 'A completed tool round must not offer tools again'
            return RunnableLambda(lambda prompt: AIMessage('Tool selection is done.'))

    prompt = ChatPromptTemplate.from_messages([
        ('system', 'Available tools: {tool_names}'), MessagesPlaceholder('messages'),
    ]).partial(tool_names='source')
    messages = [ToolMessage(MARKER, name='source', tool_call_id='original')]
    if wrap_up:
        messages.append(HumanMessage(WRAP_UP))
    result, report = take_turn(prompt, Model(), [], messages, grounded=True)
    assert result.content == report == '{"summary":"qualitative"}'
    assert 'none;' in captured[0].content
    assert MARKER in '\n'.join(str(message.content) for message in captured)
    assert messages[0].content == MARKER

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

    def turn(prompt, llm, tools, messages, grounded=False):
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
