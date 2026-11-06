from tradingagents.agents.context import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_portfolio_context_from_state,
    opponent_argument_or_opening,
    report_or_absent,
)
from tradingagents.agents.grounding import (
    evidence_from_state,
    grounded_run_enabled,
    invoke_grounded_report,
    validation_update,
)


def create_neutral_debator(llm):
    def neutral_node(state) -> dict:
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        neutral_history = risk_debate_state.get("neutral_history", "")

        current_aggressive_response = opponent_argument_or_opening(
            risk_debate_state.get("current_aggressive_response", ""), "aggressive analyst"
        )
        current_conservative_response = opponent_argument_or_opening(
            risk_debate_state.get("current_conservative_response", ""), "conservative analyst"
        )

        market_research_report = report_or_absent(state["market_report"], "market")
        sentiment_report = report_or_absent(state["sentiment_report"], "sentiment")
        news_report = report_or_absent(state["news_report"], "news")
        fundamentals_report = report_or_absent(state["fundamentals_report"], "fundamentals")
        instrument_context = get_instrument_context_from_state(state, include_evidence=not grounded_run_enabled(state))
        portfolio_context = get_portfolio_context_from_state(state)

        trader_decision = state["trader_investment_plan"]

        prompt = f"""As the Neutral Analyst, examine tradeoffs across risk scenarios as a hypothesis to test against original evidence. Assess the strongest counterevidence and disclose limitations; revise your view when sources contradict it. Do not manufacture a case for a predetermined position. Analyst reports and debate disagreements are interpretations, not independent source confirmations. Check units, periods, stock versus flow, recognition basis and missing coverage before relying on a claim.

{instrument_context}
Portfolio context: {portfolio_context}
Trader proposal (interpretation): {trader_decision}
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
Fundamentals report: {fundamentals_report}
Conversation history: {history}
Other analysts' latest responses:
{current_aggressive_response}
{current_conservative_response}

When an opponent has not spoken, offer your own assessment without inventing their claims. State which evidence supports your hypothesis, which weakens it and what remains unknown. A decisive conclusion is welcome when evidence supports it; conflicting interpretations alone do not force Hold.
""" + get_language_instruction()

        result = None
        if grounded_run_enabled(state):
            result = invoke_grounded_report(llm, prompt, evidence_from_state(state))
            content = result.content
        else:
            content = llm.invoke(prompt).content
        argument = f"Neutral Analyst: {content}"

        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "latest_speaker": "Neutral",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": argument,
            "count": risk_debate_state["count"] + 1,
        }

        update = {"risk_debate_state": new_risk_debate_state}
        if result is not None:
            update.update(validation_update('neutral', result))
        return update

    return neutral_node
