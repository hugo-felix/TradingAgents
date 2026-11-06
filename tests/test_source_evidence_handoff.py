"""Original sources must survive interpretation and missing sources cannot yield a rating."""
import json
from unittest.mock import MagicMock

import pytest

from tradingagents.agents.context import get_instrument_context_from_state
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.graph.propagation import Propagator


def state():
    return Propagator().create_initial_state('600941.SH', '2026-11-05')


@pytest.mark.parametrize('cached', ['', 'Resolved China Mobile identity'])
def test_original_source_reaches_shared_context(cached):
    current = state()
    current['instrument_context'] = cached
    current['fundamentals_evidence'] = {'fundamentals:get_cashflow:1': {
        'tool': 'get_cashflow', 'args': {'ticker': '600941.SH'},
        'status': 'available', 'content': 'Original FCF 53,869 RMB million; 2026H1',
    }}
    rendered = get_instrument_context_from_state(current)
    assert 'fundamentals:get_cashflow:1' in rendered
    assert 'Original FCF 53,869 RMB million; 2026H1' in rendered
    if cached:
        assert cached in rendered


def test_initialized_evidence_and_all_missing_blocks_portfolio_model():
    current = state()
    for role in ('market', 'fundamentals', 'news', 'sentiment'):
        assert current[f'{role}_evidence'] == {}
    current['fundamentals_evidence'] = {'fundamentals:get_cashflow:1': {
        'tool': 'get_cashflow', 'args': {}, 'status': 'error', 'content': 'Error: offline',
    }}
    llm = MagicMock()
    result = create_portfolio_manager(llm)(current)
    assert result['final_rating'] == 'REVIEW'
    assert 'REVIEW' in result['final_trade_decision']
    llm.invoke.assert_not_called()
    llm.with_structured_output.return_value.invoke.assert_not_called()


def test_available_source_reaches_portfolio_prompt():
    current = state()
    current.update(investment_plan='Debate opinion', trader_investment_plan='Trader opinion')
    current['market_evidence'] = {'market:snapshot:1': {
        'tool': 'snapshot', 'args': {}, 'status': 'available', 'content': 'Verified close 100 CNY',
        'fact_schema_version': 1, 'facts': [{'key': 'close', 'label': '收盘价', 'value': '100',
            'unit': 'CNY', 'period': '2026-11-05', 'basis': 'completed close', 'qualifiers': []}],
    }}
    llm = MagicMock()
    llm.invoke.return_value.content = json.dumps({
        'summary': '证据尚待权衡', 'rating': 'Hold', 'points': [
            {'kind': 'analysis', 'text': '价格仍需结合经营判断', 'fact_ids': ['market:snapshot:1#close']}],
    })
    result = create_portfolio_manager(llm)(current)
    assert result['final_rating'] == 'Hold'
    assert result['report_validation']['portfolio']['status'] == 'passed'
    assert 'Verified close 100 CNY' in str(llm.invoke.call_args)
    assert llm.invoke.call_count == 1


def test_research_manager_receives_source_even_when_debate_disagrees():
    from tradingagents.agents.managers.research_manager import create_research_manager
    current = state()
    current['investment_debate_state']['history'] = 'Opinion: FCF was 999'
    current['fundamentals_evidence'] = {'fundamentals:get_cashflow:1': {
        'tool': 'get_cashflow', 'args': {}, 'status': 'available',
        'content': 'Original FCF 53,869 RMB million; 2026H1',
        'fact_schema_version': 1, 'facts': [{'key': 'fcf', 'label': '自由现金流', 'value': '53869',
            'unit': 'RMB million', 'period': '2026H1', 'basis': 'reported flow', 'qualifiers': []}],
    }}
    llm = MagicMock()
    llm.invoke.return_value.content = json.dumps({
        'summary': '应以原始披露为准', 'rating': 'Hold', 'points': [
            {'kind': 'analysis', 'text': '现金流应结合其他证据判断',
             'fact_ids': ['fundamentals:get_cashflow:1#fcf']}],
    })
    result = create_research_manager(llm)(current)
    assert result['report_validation']['research_manager']['status'] == 'passed'
    prompt = str(llm.invoke.call_args)
    assert 'Opinion: FCF was 999' in prompt
    assert 'Original FCF 53,869 RMB million; 2026H1' in prompt
    assert 'Repeated opinions are not corroboration' in prompt
    assert 'fundamentals:get_cashflow:1#fcf' in result['investment_plan']
    assert '53869' not in result['investment_plan']
    assert '53869' in prompt
    assert llm.invoke.call_count == 1


def test_sentiment_evidence_reaches_context_without_replacing_core_sources():
    from tradingagents.agents.evidence import evidence_unavailable

    current = state()
    record = {'tool': 'fetch_a_share_discussions', 'args': {},
              'status': 'available', 'content': 'Original body: dividend debate'}
    current['sentiment_evidence'] = {'sentiment:fetch_a_share_discussions:1': record}
    assert 'Original body: dividend debate' in get_instrument_context_from_state(current)
    assert evidence_unavailable(current)
    current['market_evidence'] = {'market:snapshot:1': {
        'tool': 'snapshot', 'args': {}, 'status': 'available', 'content': 'Verified close 100 CNY',
    }}
    assert not evidence_unavailable(current)
