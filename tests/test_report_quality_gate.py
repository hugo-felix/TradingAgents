"""A failed fact contract cannot be published as an ordinary investment call."""
import pytest

from tradingagents.reporting import write_report_tree


def test_failed_validation_blocks_directional_report_before_writing(tmp_path):
    state = {'company_of_interest': '600941.SH', 'trade_date': '2026-11-05',
             'final_rating': 'Buy', 'final_trade_decision': 'Unvalidated decision',
             'report_validation': {'fundamentals': {'status': 'failed', 'issues': ['bad reference']}}}
    target = tmp_path / 'report'
    with pytest.raises(ValueError, match='REVIEW'):
        write_report_tree(state, '600941.SH', target)
    assert not target.exists()


def test_review_report_keeps_validation_scope_visible(tmp_path):
    state = {'company_of_interest': '600941.SH', 'trade_date': '2026-11-05',
             'final_rating': 'REVIEW', 'final_trade_decision': 'Failed validation; review evidence',
             'report_validation': {'fundamentals': {'status': 'failed', 'issues': ['bad reference']}}}
    output = write_report_tree(state, '600941.SH', tmp_path)
    assert 'Fact contract checks' in output.read_text(encoding='utf-8')
    assert 'not economic correctness' in output.read_text(encoding='utf-8')
    assert 'Fact contract checks' in (tmp_path / 'complete_report.html').read_text(encoding='utf-8')


def test_compiled_graph_keeps_legacy_state_and_merges_parallel_checks():
    from langgraph.graph import END, START, StateGraph

    from tradingagents.agents.grounding import grounded_run_enabled
    from tradingagents.agents.state import AgentState
    observed = []
    def check(role):
        def node(state):
            observed.append(grounded_run_enabled(state))
            return {'report_validation': {role: {'status': 'passed'}}}
        return node
    graph = StateGraph(AgentState)
    for role in ('market', 'news'):
        graph.add_node(role, check(role))
        graph.add_edge(START, role)
        graph.add_edge(role, END)
    compiled = graph.compile()
    legacy = {'company_of_interest': '600941.SH', 'messages': []}
    result = compiled.invoke(legacy)
    assert observed == [False, False]
    assert set(result['report_validation']) == {'market', 'news'}
    observed.clear()
    compiled.invoke({**legacy, 'report_contract_version': 1})
    assert observed == [True, True]


def test_completed_report_renders_source_facts_once_at_exit(tmp_path):
    from tradingagents.dataflows.facts import Fact, SourceResult
    _, artifact = SourceResult('Original', (
        Fact(key='net', label='5G净增', value='-1454.6', unit='万户', period='2026Q3',
             basis='quarter flow', qualifiers=('期末存量不等于期间净增',)),
        Fact(key='plan', label='资本开支计划', value=None, unit='CNY', period='2026FY',
             basis='plan', status='unverified'),
    )).as_tool_output()
    state = {'company_of_interest': '600941.SH', 'trade_date': '2026-11-05',
             'report_contract_version': 1, 'final_rating': 'Hold',
             'final_trade_decision': 'Judgment [filing#net]',
             'fundamentals_report': 'Analysis [filing#net]',
             'fundamentals_evidence': {'filing': {'tool': 'filing', 'args': {},
                 'status': 'available', 'content': 'Original', **artifact}}}
    output = write_report_tree(state, '600941.SH', tmp_path)
    content = output.read_text(encoding='utf-8')
    assert content.count('| Fact ID |') == 1
    for value in ('-1454.6', '万户', '2026Q3', 'quarter flow', '期末存量不等于期间净增', 'unverified'):
        assert value in content
        assert value in (tmp_path / 'complete_report.html').read_text(encoding='utf-8')
    assert '| Fact ID |' not in (tmp_path / '1_analysts/fundamentals.md').read_text(encoding='utf-8')
    assert (tmp_path / '6_evidence/facts.md').exists()
    write_report_tree({**state, 'fundamentals_evidence': {}}, '600941.SH', tmp_path)
    assert not (tmp_path / '6_evidence/facts.md').exists()


def test_invalid_artifact_cannot_publish_directional_fact_appendix(tmp_path):
    state = {'company_of_interest': '600941.SH', 'report_contract_version': 1,
             'final_rating': 'Buy', 'final_trade_decision': 'Judgment',
             'fundamentals_evidence': {'filing': {'tool': 'filing', 'args': {}, 'status': 'available',
                 'content': 'Original', 'fact_schema_version': 1, 'facts': [{'key': 'incomplete'}]}}}
    target = tmp_path / 'report'
    with pytest.raises(ValueError, match='Invalid source facts require REVIEW'):
        write_report_tree(state, '600941.SH', target)
    assert not target.exists()
    write_report_tree({**state, 'final_rating': 'REVIEW'}, '600941.SH', target)
    assert 'no values published' in (target / '6_evidence/facts.md').read_text(encoding='utf-8')
