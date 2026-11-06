"""Actual ToolNode artifacts reach the analyst gate and deterministic renderer."""
import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents import tools
from tradingagents.agents.analysts import fundamentals_analyst
from tradingagents.dataflows.facts import Fact, SourceResult
from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS
from tradingagents.graph.setup import _analyst_graph


@pytest.mark.parametrize('source_value,wrong_value', [('-1454.6', '-145.5'), ('35.7', '3.57')])
def test_invalid_reference_is_repaired_while_source_values_remain_in_artifacts(monkeypatch, source_value, wrong_value):
    fact = Fact(key='net', label='期间净增', value=source_value, unit='万户', period='2026Q3', basis='quarter flow')
    monkeypatch.setattr(tools, 'route_to_vendor', lambda *a, **kw: 'Legacy overview')
    monkeypatch.setattr(tools, 'route_to_vendor_result', lambda method, *a, **kw:
                        SourceResult('Original source', (fact,) if method == 'get_company_operating_history' else ()))
    draft = json.dumps({'summary': '5G连接变化仍需观察', 'points': [{'kind': 'analysis', 'text': '需要关注变化',
        'fact_ids': ['invented#' + wrong_value]}]}, ensure_ascii=False)
    monkeypatch.setattr(fundamentals_analyst, 'take_turn', lambda *a: (AIMessage(draft), draft))
    llm = Mock()
    llm.invoke.return_value = AIMessage(json.dumps({'summary': '连接变化需要跟踪', 'points': [
        {'kind': 'analysis', 'text': '结合原始净增观察变化', 'fact_ids': ['fundamentals:get_company_operating_history:1#net']},
    ]}, ensure_ascii=False))
    output = _analyst_graph(ANALYST_NODE_SPECS['fundamentals'], fundamentals_analyst.create_fundamentals_analyst(llm), 1).invoke({
        'company_of_interest': '600941.SH', 'trade_date': '2026-11-05', 'messages': [], 'report_contract_version': 1, 'report_validation': {},
    })
    assert output['report_validation']['fundamentals']['status'] == 'passed'
    assert output['report_validation']['fundamentals']['repaired']
    assert source_value not in output['fundamentals_report'] and wrong_value not in output['fundamentals_report']
    assert output['fundamentals_evidence']['fundamentals:get_company_operating_history:1']['facts'][0]['value'] == source_value
    llm.invoke.assert_called_once()


def test_failed_repair_publishes_failure_and_retains_original_evidence(monkeypatch):
    fact = Fact(key='net', label='期间净增', value='-1454.6', unit='万户', period='2026Q3', basis='quarter flow')
    monkeypatch.setattr(tools, 'route_to_vendor', lambda *a, **kw: 'Legacy overview')
    monkeypatch.setattr(tools, 'route_to_vendor_result', lambda method, *a, **kw:
                        SourceResult('Original source', (fact,) if method == 'get_company_operating_history' else ()))
    monkeypatch.setattr(fundamentals_analyst, 'take_turn', lambda *a: (AIMessage('Unvalidated 123'), 'Unvalidated 123'))
    llm = Mock()
    llm.invoke.return_value = AIMessage('Still invalid 456')
    output = _analyst_graph(ANALYST_NODE_SPECS['fundamentals'], fundamentals_analyst.create_fundamentals_analyst(llm), 1).invoke({
        'company_of_interest': '600941.SH', 'trade_date': '2026-11-05', 'messages': [], 'report_contract_version': 1, 'report_validation': {},
    })
    assert output['report_validation']['fundamentals']['status'] == 'failed'
    assert '-1454.6' not in output['fundamentals_report']
    assert output['fundamentals_evidence']['fundamentals:get_company_operating_history:1']['facts'][0]['value'] == '-1454.6'
    assert 'Unvalidated 123' not in output['fundamentals_report'] and 'Still invalid 456' not in output['fundamentals_report']
    llm.invoke.assert_called_once()


@pytest.mark.parametrize('ticker,financial_boundary', [('600941.SS', True), ('AAPL', False)])
def test_a_share_role_boundary_does_not_change_other_market_prompts(monkeypatch, ticker, financial_boundary):
    prompts = []
    def turn(prompt, *args):
        prompts.append(prompt.format_messages(messages=[])[0].content)
        return AIMessage('Analysis'), 'Analysis'
    monkeypatch.setattr(fundamentals_analyst, 'take_turn', turn)
    fundamentals_analyst.create_fundamentals_analyst(None)({
        'company_of_interest': ticker, 'trade_date': '2026-11-06', 'messages': [],
    })
    assert ('leave technical averages' in prompts[0]) == financial_boundary
