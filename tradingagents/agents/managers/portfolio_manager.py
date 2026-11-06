"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Versioned A-share runs use the grounded report contract and stop on upstream
validation failures. Legacy runs use a typed ``PortfolioDecision`` when the
provider supports it, with free-text rating parsing as their fallback.
"""

from __future__ import annotations

import html

from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
)
from tradingagents.agents.decision import DECISION_DISCIPLINE, original_analyst_views
from tradingagents.agents.evidence import evidence_unavailable
from tradingagents.agents.grounding import (
    evidence_from_state,
    grounded_run_enabled,
    invoke_grounded_report,
    validation_update,
)
from tradingagents.agents.rating import RATING_REVIEW, RATINGS_5_TIER, parse_rating
from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.structured import NO_EXTERNAL_TOOLS, bind_structured, invoke_structured


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        if grounded_run_enabled(state):
            failed = {role: entry for role, entry in state['report_validation'].items()
                      if entry.get('status') == 'failed'}
            if failed:
                issues = [f"{role}: {issue}" for role, entry in failed.items()
                          for issue in (entry.get('issues') or ['Grounded report validation failed'])]
                return {
                    'risk_debate_state': {**state.get('risk_debate_state', {}), 'latest_speaker': 'Judge'},
                    'final_trade_decision': '**Rating**: REVIEW\n\n上游报告校验失败，暂不形成投资评级。\n'
                        + '\n'.join(html.escape(issue) for issue in issues)
                        + '\nValidation scope: references, value display and known qualifications; not economic correctness.',
                    'final_rating': RATING_REVIEW,
                    'report_validation': {'portfolio': {'status': 'failed', 'issues': issues, 'repaired': False}},
                }
        if evidence_unavailable(state):
            return {
                "risk_debate_state": {**state.get("risk_debate_state", {}), "latest_speaker": "Judge"},
                "final_trade_decision": (
                    "**Rating**: REVIEW\n\n"
                    "本次 A 股分析没有取得可用的行情、财务或新闻原始数据，暂不形成投资评级。"
                    "人气和社区观点不能单独支撑投资评级。"
                    "请检查来源覆盖、数据接口及分析日期后再评估。\n"
                    "No usable market, fundamentals or news source data; social attention and opinions alone cannot support a rating."
                ),
                "final_rating": RATING_REVIEW,
            }
        instrument_context = get_instrument_context_from_state(state, include_evidence=not grounded_run_enabled(state))
        portfolio_context = get_portfolio_context_from_state(state)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

{portfolio_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Check original source facts, units, periods and qualifications for every conclusion. Analyst claims and disagreements are interpretations, not independent source confirmations. Disclose the strongest counterevidence and limitations. The risk debate always contains conflicting stances; deciding which is stronger is the job, so conflict alone is not a reason to Hold. Commit to the stronger case, sized by how decisively it wins. Choose Hold only when the evidence is still balanced after that weighing, or too thin to support a call; do not force a direction to appear decisive. Weigh the analysts on their merits, independent of speaking order.

{DECISION_DISCIPLINE}

{original_analyst_views(state)}

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        # The typed rating is the decision; the rendered text only carries it.
        # Read back from text, a rating the thesis quotes could replace it.
        result = None
        if grounded_run_enabled(state):
            result = invoke_grounded_report(llm, prompt, evidence_from_state(state), RATINGS_5_TIER, require_decision_plan=True)
            final_trade_decision = result.content
            final_rating = result.rating or RATING_REVIEW
        else:
            prompt += """
## Output

Write these sections, in this order, starting with the rating on its own line:

- **Rating**: exactly one of Buy / Overweight / Hold / Underweight / Sell
- **Executive Summary**: the call and how to act on it
- **Investment Thesis**: the evidence that decided it, and what would change it
- **配置期限**, **执行方式**, **执行条件**, **计划有效期**, **失效条件**, **最强反向证据**, **事件窗口**, **市场与板块**: separate, explicit fields; mark unavailable evidence rather than guessing

"""
            decision = invoke_structured(structured_llm, prompt, "Portfolio Manager")
            if decision is not None:
                final_trade_decision = render_pm_decision(decision)
                final_rating = decision.rating.value
            else:
                final_trade_decision = llm.invoke(prompt).content
                final_rating = parse_rating(final_trade_decision)

        new_risk_debate_state = {
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        update = {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
            "final_rating": final_rating,
        }
        if result is not None:
            update.update(validation_update('portfolio', result))
        return update

    return portfolio_manager_node
