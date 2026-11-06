import logging
from collections import Counter
from datetime import date, timedelta
from typing import Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.analysts.turn import WRAP_UP
from tradingagents.agents.evidence import (
    EVIDENCE_KEYS,
    PREFETCH_MARKER,
    collect_tool_evidence,
    source_coverage,
)
from tradingagents.agents.state import AgentState
from tradingagents.dataflows.symbols import is_a_share

from .analyst_execution import build_analyst_execution_plan
from .conditional_logic import ConditionalLogic

logger = logging.getLogger(__name__)

# Every target a shared conditional router can return. Each edge driven by the
# router maps all of them, so a fall-through return (e.g. under prompt/i18n/
# refactor drift in the speaker labels) can never hit a missing path_map entry
# and crash LangGraph mid-run (#1088).
DEBATE_PATH_MAP = {
    "Bull Researcher": "Bull Researcher",
    "Bear Researcher": "Bear Researcher",
    "Research Manager": "Research Manager",
}
RISK_ANALYSIS_PATH_MAP = {
    "Aggressive Analyst": "Aggressive Analyst",
    "Conservative Analyst": "Conservative Analyst",
    "Neutral Analyst": "Neutral Analyst",
    "Portfolio Manager": "Portfolio Manager",
}


def _tools_or_done(state) -> str:
    """Route an analyst's turn: run its tool calls, or finish with its report."""
    return "tools" if state["messages"][-1].tool_calls else END


def _required_calls(role, trade_date, *, prefetch_disclosures=True):
    """Deterministic minimum source set. Identity/date still come from ToolNode state."""
    day = date.fromisoformat(trade_date)
    current = {"curr_date": trade_date}
    disclosures = ("get_company_disclosures", {**current, "look_back_days": 730, "limit": 6})
    required = {
        "market": [("get_stock_data", {"start_date": (day - timedelta(days=400)).isoformat(), "end_date": trade_date}),
                   ("get_verified_market_snapshot", current)],
        "fundamentals": [("get_fundamentals", current),
                         *[(name, {**current, "freq": "quarterly"}) for name in
                           ("get_balance_sheet", "get_cashflow", "get_income_statement")],
                         disclosures, ("get_dividends", current),
                         ("get_company_operating_history", current),
                         ("get_valuation_comparison", {**current, "look_back_years": 3})],
        "news": [("get_news", {"start_date": (day - timedelta(days=7)).isoformat(), "end_date": trade_date}), disclosures],
    }
    calls = required.get(role, [])
    if not prefetch_disclosures:
        calls = [(name, args) for name, args in calls if name != 'get_company_disclosures']
    return [{"name": name, "args": args, "id": f"prefetch:{role}:{index}"}
            for index, (name, args) in enumerate(calls)]


def _analyst_graph(spec, agent, max_tool_rounds: int, *, prefetch_disclosures=True):
    """One analyst as a graph of its own: the model and its tools, on a private message history.

    It returns its report and role-specific source records, so parallel analysts
    never write the same key, and its tool calls never reach the other analysts' messages. After
    ``max_tool_rounds`` rounds of tool calls it is told to write its report, and
    that turn ends it whatever it answers, so a model that keeps calling tools
    cannot run the graph into its recursion limit (#1420).
    """
    evidence_key = EVIDENCE_KEYS.get(spec.key)
    fields = {spec.report_key: str}
    if evidence_key:
        fields[evidence_key] = dict
    fields['report_validation'] = dict
    output = TypedDict(f"{spec.key.capitalize()}Report", fields)
    graph = StateGraph(AgentState, output_schema=output)
    graph.add_edge(START, "agent")
    if not spec.tools:
        graph.add_node("agent", agent)
        graph.add_edge("agent", END)
        return graph.compile()

    # Catch source failures as real ToolMessages rather than aborting every analyst.
    tool_node = ToolNode(list(spec.tools), handle_tool_errors=True)
    legacy_tool_node = ToolNode(list(spec.tools))
    tools_by_name = {tool.name: tool for tool in spec.tools}

    def run_tools(state):
        if not evidence_key or not is_a_share(state["company_of_interest"]):
            return legacy_tool_node.invoke(state)
        result = tool_node.invoke(state)
        requested = {call["id"]: call for call in state["messages"][-1].tool_calls}
        for message in result["messages"]:
            call = requested.get(message.tool_call_id, {})
            tool = tools_by_name.get(call.get("name"))
            args = dict(call.get("args", {}))
            if tool is not None:
                # These fields are injected by the actual ToolNode, never trusted
                # from the model. Retain them alongside the requested arguments.
                properties = tool.get_input_schema().model_fields
                for name in ("ticker", "symbol"):
                    if name in properties:
                        args[name] = state["company_of_interest"]
                if "trade_date" in properties:
                    args["trade_date"] = state["trade_date"]
            message.additional_kwargs["source_args"] = args
        return result

    def analyst_turn(state):
        messages = list(state["messages"])
        mainland = evidence_key and is_a_share(state["company_of_interest"])
        prefetched = []
        if mainland and not any(m.additional_kwargs.get(PREFETCH_MARKER) for m in messages):
            request = AIMessage("", tool_calls=_required_calls(
                spec.key, state["trade_date"], prefetch_disclosures=prefetch_disclosures),
                                additional_kwargs={PREFETCH_MARKER: True})
            prefetched = [request, *run_tools({**state, "messages": [*messages, request]})["messages"]]
            messages.extend(prefetched)
        private_state = {**state, "messages": messages}
        if mainland:
            private_state[evidence_key] = collect_tool_evidence(spec.key, messages)
        result = agent(private_state)
        result = {**result, "messages": [*prefetched, *result.get("messages", [])]}
        if mainland:
            evidence = collect_tool_evidence(spec.key, [*messages, *result["messages"][len(prefetched):]])
            result[evidence_key] = evidence
            if result.get(spec.report_key):
                result[spec.report_key] += source_coverage(evidence)
        return result

    graph.add_node("agent", analyst_turn)

    def calls(messages):
        return [call["name"] for m in messages for call in (getattr(m, "tool_calls", None) or [])]

    def rounds(messages) -> int:
        return sum(1 for m in messages if getattr(m, "tool_calls", None)
                   and not m.additional_kwargs.get(PREFETCH_MARKER))

    def more_or_wrap_up(state) -> str:
        return "wrap_up" if rounds(state["messages"]) >= max_tool_rounds else "agent"

    def wrap_up(state):
        repeated = ", ".join(f"{name} x{n}" for name, n in Counter(calls(state["messages"])).most_common())
        logger.warning("%s used its %d tool rounds (%s); asking for its report",
                       spec.agent_node, max_tool_rounds, repeated)
        return analyst_turn({**state, "messages": [*state["messages"], HumanMessage(WRAP_UP)]})

    graph.add_node("tools", run_tools)
    graph.add_node("wrap_up", wrap_up)
    graph.add_conditional_edges("agent", _tools_or_done, ["tools", END])
    graph.add_conditional_edges("tools", more_or_wrap_up, ["agent", "wrap_up"])
    graph.add_edge("wrap_up", END)
    return graph.compile()


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        conditional_logic: ConditionalLogic,
        max_tool_rounds: int,
    ):
        """Initialize with required components."""
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.conditional_logic = conditional_logic
        self.max_tool_rounds = max_tool_rounds

    def setup_graph(
        self, selected_analysts=("market", "social", "news", "fundamentals"), memory_node=None
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst
                - "social": Sentiment analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
            memory_node: Node that settles past decisions and returns the run's
                ``past_context``. It runs alongside the analysts.
        """
        plan = build_analyst_execution_plan(selected_analysts)
        disclosure_role = 'fundamentals' if any(s.key == 'fundamentals' for s in plan.specs) else 'news'

        analyst_factories = {
            "market": lambda: create_market_analyst(self.quick_thinking_llm),
            "social": lambda: create_sentiment_analyst(self.quick_thinking_llm),
            "news": lambda: create_news_analyst(self.quick_thinking_llm),
            "fundamentals": lambda: create_fundamentals_analyst(self.quick_thinking_llm),
        }

        bull_researcher_node = create_bull_researcher(self.quick_thinking_llm)
        bear_researcher_node = create_bear_researcher(self.quick_thinking_llm)
        research_manager_node = create_research_manager(self.deep_thinking_llm)
        trader_node = create_trader(self.quick_thinking_llm)

        aggressive_analyst = create_aggressive_debator(self.quick_thinking_llm)
        neutral_analyst = create_neutral_debator(self.quick_thinking_llm)
        conservative_analyst = create_conservative_debator(self.quick_thinking_llm)
        portfolio_manager_node = create_portfolio_manager(self.deep_thinking_llm)

        workflow = StateGraph(AgentState)

        for spec in plan.specs:
            workflow.add_node(spec.agent_node,
                              _analyst_graph(spec, analyst_factories[spec.key](), self.max_tool_rounds,
                                             prefetch_disclosures=spec.key == disclosure_role))

        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # The analysts work at the same time; the research debate starts once
        # every one of them has filed its report. The memory log settles past
        # decisions alongside them: only the Portfolio Manager reads its lessons.
        first_steps = [spec.agent_node for spec in plan.specs]
        if memory_node is not None:
            workflow.add_node("Memory Log", memory_node)
            first_steps.append("Memory Log")
        for node in first_steps:
            workflow.add_edge(START, node)
        workflow.add_edge(first_steps, "Bull Researcher")

        # Both research-debate edges share the complete DEBATE_PATH_MAP (#1088).
        for debate_node in ("Bull Researcher", "Bear Researcher"):
            workflow.add_conditional_edges(
                debate_node,
                self.conditional_logic.should_continue_debate,
                DEBATE_PATH_MAP,
            )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        # All three risk edges share the complete RISK_ANALYSIS_PATH_MAP (#1088).
        for risk_node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
            workflow.add_conditional_edges(
                risk_node,
                self.conditional_logic.should_continue_risk_analysis,
                RISK_ANALYSIS_PATH_MAP,
            )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
