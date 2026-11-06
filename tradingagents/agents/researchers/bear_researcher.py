from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    opponent_argument_or_opening,
    report_or_absent,
)
from tradingagents.agents.grounding import (
    evidence_from_state,
    grounded_run_enabled,
    invoke_grounded_report,
    validation_update,
)


def create_bear_researcher(llm):
    def bear_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bear_history = investment_debate_state.get("bear_history", "")

        current_response = opponent_argument_or_opening(
            investment_debate_state.get("current_response", ""), "bull analyst"
        )
        market_research_report = report_or_absent(state["market_report"], "market")
        sentiment_report = report_or_absent(state["sentiment_report"], "sentiment")
        news_report = report_or_absent(state["news_report"], "news")
        fundamentals_report = report_or_absent(state["fundamentals_report"], "fundamentals")
        instrument_context = get_instrument_context_from_state(state, include_evidence=not grounded_run_enabled(state))

        prompt = f"""As the Bear Analyst, examine downside exposure as a hypothesis to test against original evidence. Assess the strongest counterevidence and disclose limitations; revise your view when sources contradict it. Do not manufacture a case for a predetermined position. Analyst reports and debate disagreements are interpretations, not independent source confirmations. Check units, periods, stock versus flow, recognition basis and missing coverage before relying on a claim.

{instrument_context}
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
Fundamentals report: {fundamentals_report}
Conversation history: {history}
Other analysts' latest responses:
{current_response}

When an opponent has not spoken, offer your own assessment without inventing their claims. State which evidence supports your hypothesis, which weakens it and what remains unknown. A decisive conclusion is welcome when evidence supports it; conflicting interpretations alone do not force Hold.
""" + get_language_instruction()

        result = None
        if grounded_run_enabled(state):
            result = invoke_grounded_report(llm, prompt, evidence_from_state(state))
            content = result.content
        else:
            content = llm.invoke(prompt).content
        argument = f"Bear Analyst: {content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        update = {"investment_debate_state": new_investment_debate_state}
        if result is not None:
            update.update(validation_update('bear', result))
        return update

    return bear_node
