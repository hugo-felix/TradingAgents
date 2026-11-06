from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import finish_analyst_turn, report_instruction, take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.tools import (
    get_balance_sheet,
    get_cashflow,
    get_company_disclosures,
    get_company_operating_history,
    get_dividends,
    get_fundamentals,
    get_income_statement,
    get_insider_transactions,
    get_valuation_comparison,
)
from tradingagents.dataflows.symbols import is_a_share

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_insider_transactions,
    get_company_disclosures,
    get_dividends,
    get_company_operating_history,
    get_valuation_comparison,
)


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state, include_evidence=False)
        mainland = is_a_share(state["company_of_interest"])
        available_tools = TOOLS if mainland else tuple(t for t in TOOLS if t not in (
            get_company_disclosures, get_dividends, get_company_operating_history, get_valuation_comparison,
        ))

        system_message = (
            "You are a researcher tasked with analyzing fundamental information over the past week about a company. Please write a comprehensive report of the company's fundamental information such as financial documents, company profile, basic company financials, and company financial history to gain a full view of the company's fundamental information to inform traders. Make sure to include as much detail as possible. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + " Use the available tools: `get_fundamentals` for comprehensive company analysis, `get_balance_sheet`, `get_cashflow`, and `get_income_statement` for specific financial statements, and `get_insider_transactions` for recent insider buying and selling."
            + " When tools supply code-calculated financial growth and standalone quarters, cite those comparisons with their metric, period basis and units. Do not calculate replacements for comparisons explicitly marked missing or rejected. For other sources, any derived comparison must identify its inputs and matching accounting basis. Never mix parent-attributable profit with consolidated net profit, currencies, units or cumulative periods. Balance-sheet balances are point-in-time values; EPS cannot be differenced to obtain quarterly EPS. A zero or negative prior-year base does not support an ordinary growth percentage."
            + " Net investing cash flow is not capital expenditure. Cash paid to acquire or construct long-term assets (购建长期资产支付现金) is that statement line only; do not call it company-disclosed capex without a separate matching disclosure. Dividends/profit distributions and interest paid includes interest and cannot establish pure dividends or a dividend payout ratio. State missing data and accounting-basis gaps explicitly; do not assert capex, payout ratios, or financial trends unsupported by comparable evidence. Historical statements withheld by the tool must remain unavailable."
            + get_language_instruction()
        )
        if mainland:
            system_message += (
                " Prefer results already supplied for this ticker, date and range. For this mainland A-share,"
                " use get_company_disclosures results for official announcements, source excerpts and supported"
                " operating metrics, and get_dividends results for the distribution scheme. Call these tools"
                " only when the needed result is missing or a different range is required."
                " Preserve every metric's reporting period, unit, source and publication date. Half-year ARPU"
                " is not a third-quarter value; company payout guidance is not a realized ratio or guarantee."
                " Dividend cash per share is gross/tax-inclusive unless stated otherwise; compare A-share"
                " currency and scheme stage against the official implementation disclosure. PDF excerpts"
                " can be partial. If a metric or chart was not parsed, report the gap instead of reconstructing it."
                " Use supplied get_company_operating_history records for company-disclosed actual capex and"
                " multi-period operating data where supported. Keep H1 averages separate from quarter-end"
                " customer balances. Preserve VAT/statistical-definition changes; do not infer adjusted ARPU"
                " growth or splice wireline broadband with expanded broadband-connection definitions. Actual"
                " capex, capital commitments and future plans are different; a missing full-year plan must"
                " remain missing, and half-year capex must not be annualized into a company plan."
                " Use get_valuation_comparison for historical PE(TTM)/PB and supported same-day A-share peers."
                " Retain sample dates/counts, missing or nonpositive metrics, and the percentile method. A"
                " percentile is relative to the observed window, not proof of fair value; different peer"
                " business, ownership and accounting structures limit direct comparisons. Do not mix static"
                " PE with TTM PE, or blend different providers. Missing/stale peer dates stay missing."
            )

            system_message += " Limit this report to financial, operating and valuation analysis; leave technical averages, price levels and execution triggers to the Market Analyst."
        task_context = system_message + "\n" + instrument_context
        system_message += report_instruction(state)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in available_tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        result, report = take_turn(prompt, llm, available_tools, state["messages"])

        result, report, checks = finish_analyst_turn(state, llm, result, report, "fundamentals",
                                                    task_context=task_context)

        return {
            "messages": [result],
            "fundamentals_report": report,
            **checks,
        }

    return fundamentals_analyst_node
