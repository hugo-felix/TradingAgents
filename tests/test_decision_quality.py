"""Regression cases from the 600941 attribution: allocation is not a market order."""
import json

import pytest

from tests.test_grounded_downstream import Model, state
from tests.test_grounding import Model as Responses, evidence, payload
from tradingagents.agents.grounding import finish_grounded_report, validate_grounded_report
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager


def plan():
    return {
        'fact_ids': ['filing#net'],
        'allocation_horizon': '一个季度，复核下次业绩披露',
        'execution_mode': 'conditional',
        'execution_conditions': '等待有依据的反弹条件；未确认前不执行。',
        'valid_until': '下一次业绩披露或事件窗口发生变化时重审。',
        'invalidation_conditions': '关键经营指标改善或趋势假设失效。',
        'counterevidence': '现金流支持仍在，应保留反向情景。',
        'catalysts': '事件日历覆盖不足，不能断言没有催化。',
        'benchmark_context': '缺少同期板块与指数收益，不做相对强弱归因。',
    }


def test_final_decision_requires_plan_and_preserves_conditional_execution():
    raw = payload(rating='Underweight')
    with pytest.raises(ValueError, match='decision_plan'):
        validate_grounded_report(raw, evidence(), ('Underweight',), require_decision_plan=True)
    raw['decision_plan'] = plan()
    report = validate_grounded_report(raw, evidence(), ('Underweight',), require_decision_plan=True)
    assert report.decision_plan.execution_mode == 'conditional'
    result = finish_grounded_report(Responses([]), raw, evidence(), ('Underweight',), require_decision_plan=True)
    assert result.status == 'passed'
    assert '**执行方式**：条件触发' in result.content
    assert '**配置期限**' in result.content and '**计划有效期**' in result.content
    assert '**失效条件**' in result.content and '**事件窗口**' in result.content


@pytest.mark.parametrize('field', list(plan()))
def test_missing_or_blank_plan_fields_are_not_executable(field):
    raw = payload(rating='Underweight')
    raw['decision_plan'] = plan()
    raw['decision_plan'][field] = '   '
    result = finish_grounded_report(Responses(['invalid']), raw, evidence(), ('Underweight',), require_decision_plan=True)
    assert result.status == 'failed' and result.rating == 'REVIEW'
    assert '**执行方式**' not in result.content


def test_one_repair_receives_required_plan_contract():
    valid = payload(rating='Underweight')
    valid['decision_plan'] = plan()
    llm = Responses([json.dumps(valid)])
    result = finish_grounded_report(llm, payload(rating='Underweight'), evidence(), ('Underweight',), require_decision_plan=True)
    assert result.status == 'passed' and result.repaired
    assert 'decision_plan' in llm.calls[0] and 'required' in llm.calls[0]
    assert len(llm.calls) == 1


def test_pm_cannot_publish_bare_rating_even_with_valid_fact_references():
    # Old generic model fixture gives no plan. Both attempts must fail closed.
    class BareModel(Model):
        def invoke(self, prompt):
            result = super().invoke(prompt)
            raw = json.loads(result.content)
            raw.pop('decision_plan', None)
            result.content = json.dumps(raw)
            return result
    result = create_portfolio_manager(BareModel('Underweight'))(state())
    assert result['final_rating'] == 'REVIEW'
    assert result['report_validation']['portfolio']['status'] == 'failed'


@pytest.mark.parametrize('factory', [create_research_manager, create_portfolio_manager])
def test_managers_see_original_analyst_views_not_only_debate_summary(factory):
    raw = state()
    raw['market_report'] = 'UNIQUE ORIGINAL TECHNICAL HOLD / short pullback, long uptrend'
    raw['fundamentals_report'] = 'UNIQUE ORIGINAL FUNDAMENTAL HOLD / cash flow buffer'
    raw['news_report'] = 'UNIQUE ORIGINAL NEWS NEUTRAL / event window uncertain'
    raw['sentiment_report'] = 'UNIQUE ORIGINAL SENTIMENT UNASSESSED / attention not direction'
    llm = Model('Underweight')
    factory(llm)(raw)
    for key in ('market_report', 'fundamentals_report', 'news_report', 'sentiment_report'):
        assert raw[key] in llm.calls[0]
    assert 'not independent votes' in llm.calls[0]
    assert 'next-day' in llm.calls[0] and 'standalone quarter' in llm.calls[0]


@pytest.mark.parametrize('claim', ['净增为-145.5万户。', '净增为-1454.60户。'])
def test_live_pipeline_rejects_changed_measurement_value_or_unit(claim):
    from tradingagents.agents.grounding import invoke_grounded_report
    raw = payload()
    raw['points'][0]['text'] = claim
    result = invoke_grounded_report(Responses([json.dumps(raw), json.dumps(raw)]), 'Analyze', evidence())
    assert result.status == 'failed'
    assert claim not in result.content
    assert any('measurement' in issue.lower() for issue in result.issues)


def test_live_pipeline_retains_correct_measurement_with_immutable_unit():
    from tradingagents.agents.grounding import invoke_grounded_report
    raw = payload()
    raw['points'][0]['text'] = '净增为-1454.60万户，仍需结合其他指标。'
    result = invoke_grounded_report(Responses([json.dumps(raw)]), 'Analyze', evidence())
    assert result.status == 'passed'


def test_generated_risk_prompt_applies_shared_decision_discipline():
    from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
    llm = Model()
    create_aggressive_debator(llm)(state())
    assert 'not independent votes' in llm.calls[0]
    assert 'current-period expense recognition' in llm.calls[0]
    assert 'Attention rank' in llm.calls[0]


def test_final_plan_rejects_unknown_evidence_references():
    raw = payload(rating='Underweight')
    raw['decision_plan'] = {**plan(), 'fact_ids': ['invented#price']}
    result = finish_grounded_report(Responses(['invalid']), raw, evidence(), ('Underweight',), require_decision_plan=True)
    assert result.status == 'failed'
    assert any('unknown' in issue for issue in result.issues)
