"""Research Manager: turns the bull/bear debate into a structured investment plan for the trader."""

from __future__ import annotations

from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.decision import DECISION_DISCIPLINE, original_analyst_views
from tradingagents.agents.grounding import (
    evidence_from_state,
    grounded_run_enabled,
    invoke_grounded_report,
    validation_update,
)
from tradingagents.agents.rating import RATINGS_5_TIER
from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    def research_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state, include_evidence=not grounded_run_enabled(state))
        history = state["investment_debate_state"].get("history", "")

        investment_debate_state = state["investment_debate_state"]

        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

The debate always contains conflicting arguments; deciding which side is stronger is the job, so conflict alone is not a reason to Hold. Commit to the side with the stronger case, sized by how decisively it wins. Choose Hold only when the evidence is still balanced after that weighing, or too thin to support a call; do not manufacture a direction to appear decisive. Weigh the bull and bear cases on their merits, independent of which side spoke first or last.
Check original facts, their units, periods and qualifications before relying on an argument. Report the strongest counterevidence and limitations. Disagreements and repeated analyst claims are interpretations, not additional source confirmations.

---

**Debate History:**
{history}

{DECISION_DISCIPLINE}

{original_analyst_views(state)}

{NO_EXTERNAL_TOOLS}""" + get_language_instruction()

        result = None
        if grounded_run_enabled(state):
            result = invoke_grounded_report(llm, prompt, evidence_from_state(state), RATINGS_5_TIER)
            investment_plan = result.content
        else:
            prompt += """
## Output

Write these sections, in this order, starting with the recommendation on its own line:

- **Recommendation**: exactly one of Buy / Overweight / Hold / Underweight / Sell
- **Rationale**: which arguments decided it
- **Strategic Actions**: concrete steps for the trader, sized against a standard allocation

"""
            investment_plan = invoke_structured_or_freetext(
                structured_llm, llm, prompt, render_research_plan, "Research Manager",
            )

        new_investment_debate_state = {
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        update = {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }
        if result is not None:
            update.update(validation_update('research_manager', result))
        return update

    return research_manager_node
