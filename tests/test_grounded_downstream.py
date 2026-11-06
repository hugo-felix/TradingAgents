"""Versioned A-share runs keep downstream reasoning inside the grounded contract."""
import json
from types import SimpleNamespace

import pytest

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
from tradingagents.agents.trader.trader import create_trader


class Model:
    def __init__(self, rating=None, valid=True):
        self.calls = []
        self.rating = rating
        self.valid = valid

    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, prompt):
        self.calls.append(prompt)
        raw = {'summary': '经营变化仍需关注', 'points': [
            {'kind': 'analysis', 'text': '变化可能影响经营', 'fact_ids': ['filing#net']}]}
        if self.rating:
            raw['rating'] = self.rating
        return SimpleNamespace(content=json.dumps(raw, ensure_ascii=False) if self.valid else 'INVALID DRAFT')


def state():
    return {'company_of_interest': '600941.SH', 'asset_type': 'stock', 'report_contract_version': 1, 'report_validation': {},
            **dict.fromkeys(['market_report', 'fundamentals_report', 'news_report', 'sentiment_report', 'investment_plan', 'trader_investment_plan'], 'analysis'),
            'fundamentals_evidence': {'filing': {'tool': 'filing', 'args': {}, 'status': 'available',
                'content': 'original', 'facts': [{'key': 'net', 'label': '净增', 'value': '-1454.60', 'unit': '万户',
                'period': '2026Q3', 'basis': 'quarter flow', 'qualifiers': ['存量不等于净增']}]}},
            'investment_debate_state': {'history': '', 'bull_history': '', 'bear_history': '', 'current_response': '', 'count': 0},
            'risk_debate_state': {'history': '', 'aggressive_history': '', 'conservative_history': '', 'neutral_history': '',
                'current_aggressive_response': '', 'current_conservative_response': '', 'current_neutral_response': '', 'count': 0}}


ROLES = [(create_bull_researcher, 'bull', None), (create_bear_researcher, 'bear', None),
         (create_aggressive_debator, 'aggressive', None), (create_conservative_debator, 'conservative', None),
         (create_neutral_debator, 'neutral', None), (create_research_manager, 'research_manager', 'Overweight'),
         (create_trader, 'trader', 'Buy'), (create_portfolio_manager, 'portfolio', 'Overweight')]


@pytest.mark.parametrize('factory,role,rating', ROLES)
def test_grounded_roles_keep_compact_references_and_record_validation(factory, role, rating):
    llm = Model(rating)
    result = factory(llm)(state())
    assert result['report_validation'][role]['status'] == 'passed'
    assert 'filing#net' in str(result)
    assert '| Fact ID |' not in str(result)
    assert '-1454.60' in llm.calls[0] and '存量不等于净增' in llm.calls[0]
    assert len(llm.calls) == 1
    if role == 'portfolio':
        assert result['final_rating'] == 'Overweight'


@pytest.mark.parametrize('factory,role,rating', ROLES)
def test_invalid_draft_is_not_promoted_after_single_repair(factory, role, rating):
    llm = Model(rating, valid=False)
    result = factory(llm)(state())
    assert result['report_validation'][role]['status'] == 'failed'
    assert 'INVALID DRAFT' not in str(result)
    assert len(llm.calls) == 2
    if role == 'portfolio':
        assert result['final_rating'] == 'REVIEW'


def test_portfolio_stops_before_generation_when_any_upstream_validation_failed():
    raw = state()
    raw['report_validation'] = {'news': {'status': 'failed', 'issues': ['unsupported reference'], 'repaired': True}}
    llm = Model('Buy')
    result = create_portfolio_manager(llm)(raw)
    assert result['final_rating'] == 'REVIEW' and not llm.calls
    assert result['risk_debate_state']['count'] == 0
    assert 'unsupported reference' in result['final_trade_decision']


def test_unversioned_a_share_state_preserves_legacy_invocation():
    raw = state()
    raw.pop('report_contract_version')
    raw.pop('report_validation')
    llm = Model(valid=False)
    result = create_bull_researcher(llm)(raw)
    assert 'report_validation' not in result
    assert 'INVALID DRAFT' in result['investment_debate_state']['current_response']
    assert len(llm.calls) == 1


def test_non_a_share_state_preserves_legacy_invocation():
    raw = state()
    raw['company_of_interest'] = 'AAPL'
    llm = Model(valid=False)
    result = create_bull_researcher(llm)(raw)
    assert 'report_validation' not in result
    assert len(llm.calls) == 1


@pytest.mark.parametrize('factory,role,rating', ROLES)
def test_every_role_prompt_requires_counterevidence_and_original_sources(factory, role, rating):
    llm = Model(rating)
    factory(llm)(state())
    prompt = llm.calls[0].lower()
    assert 'counterevidence' in prompt and 'limitations' in prompt
    assert 'original' in prompt and 'interpretation' in prompt
    assert 'compelling case for the trader' not in prompt


def test_trader_cannot_silently_accept_manager_rating_vocabulary():
    llm = Model('Overweight')
    result = create_trader(llm)(state())
    assert result['report_validation']['trader']['status'] == 'failed'
    assert 'Investment Rating: REVIEW' in result['trader_investment_plan']
    assert len(llm.calls) == 2


def test_pm_keeps_no_source_guard_without_model_call():
    raw = state()
    raw['fundamentals_evidence']['filing']['status'] = 'error'
    llm = Model('Buy')
    result = create_portfolio_manager(llm)(raw)
    assert result['final_rating'] == 'REVIEW'
    assert not llm.calls


@pytest.mark.parametrize('factory,role,rating', ROLES)
def test_grounded_prompt_has_one_output_contract(factory, role, rating):
    llm = Model(rating)
    factory(llm)(state())
    prompt = llm.calls[0]
    assert 'Return ONLY a JSON object matching this schema:' in prompt
    assert 'Write these sections' not in prompt
    assert '## Output' not in prompt
    assert 'on its own line' not in prompt
    assert 'without any special formatting' not in prompt
    assert '**Entry Price**' not in prompt


@pytest.mark.parametrize('factory,role,rating', ROLES)
def test_source_and_fact_catalog_appear_once_in_generation_and_repair(factory, role, rating):
    raw = state()
    raw['fundamentals_evidence']['filing']['content'] = 'UNIQUE ORIGINAL SOURCE BODY'
    llm = Model(rating, valid=False)
    factory(llm)(raw)
    for prompt in llm.calls:
        assert prompt.count('UNIQUE ORIGINAL SOURCE BODY') == 1
        assert prompt.count('Canonical source facts:') == 1
