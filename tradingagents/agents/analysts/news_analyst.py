from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import finish_analyst_turn, report_instruction, take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.tools import (
    get_company_disclosures,
    get_event_candidates,
    get_global_news,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)
from tradingagents.dataflows.symbols import is_a_share

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_news,
    get_event_candidates,
    get_global_news,
    get_macro_indicators,
    get_prediction_markets,
    get_company_disclosures,
)


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state, include_evidence=False)
        mainland = is_a_share(state["company_of_interest"])
        available_tools = TOOLS if mainland else tuple(t for t in TOOLS if t not in (get_company_disclosures, get_event_candidates))

        system_message = (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(start_date, end_date) for news about the {asset_label} under analysis, get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news, get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve'), and get_prediction_markets(topic, limit) for live market-implied probabilities of forward-looking events (e.g. 'Fed rate cut', 'recession 2026', geopolitical or sector events). Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + " Distinguish sourced company facts, industry/macro background, and your own inference explicitly."
            " Cite the source and date for company facts and verify that the article concerns this instrument."
            " Global news and other companies' financing or AI spending are background; they do not prove"
            " this company's orders or profits will benefit without direct supporting evidence."
            " Label any proposed transmission mechanism as inference and explain what evidence is missing."
            " Group multiple reports about the same event instead of counting reprints as independent catalysts."
            " Distinguish the listed issuer from its parent group, subsidiaries and other share classes."
            " Identify buyers and sellers correctly: a supplier winning the issuer's procurement tender"
            " is not evidence that the issuer won a sales order."
            " If get_news returns DATA_UNAVAILABLE or insufficient coverage, state that company news could"
            " not be verified; do not conclude there were no company events."
            " Do not infer a trading catalyst from sensitive political material without a direct,"
            " sourced connection to the instrument."
            + get_language_instruction()
        )
        if mainland:
            system_message += (
                " Use get_event_candidates(curr_date) or its prefetched result to inspect older preannouncements."
                " Build an explicit ongoing/upcoming event window for the next 14 calendar days, retaining"
                " relevant later events for the allocation horizon. Cite each source and separate publication"
                " date from event date. Date-ambiguous or past events are not confirmed upcoming catalysts."
                " No candidates, failed requests or a sparse recent sample mean incomplete calendar coverage,"
                " not no catalysts. Never use news published after the analysis date."
                " Procurement is not evidence of current-period expense recognition: distinguish investment,"
                " future growth expectations and realized revenue; examine both interpretations."
                " Prefer results already supplied for this ticker, date and range. Use supplied official"
                " disclosure results when available. Do not fetch a broad announcement review merely to"
                " duplicate the Fundamentals Analyst. Call get_company_disclosures only to verify a specific"
                " news event or fill missing company-news evidence; do not claim to have read unseen results."
                " A disclosure can be older than the weekly news window: keep its publication date and"
                " reporting period explicit instead of presenting it as a new weekly event."
                " For domestic macro context use china_cpi, china_pmi, china_gdp, china_lpr and china_credit"
                " with get_macro_indicators. Unprefixed cpi/fed_funds_rate are overseas series, not China."
                " GDP and credit can be cumulative; a data period is not its publication date. Historical"
                " series withheld by the tool are unavailable, not an invitation to guess."
                " Distinguish disabled sources, missing credentials, request failures and insufficient coverage."
            )

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

        result, report, checks = finish_analyst_turn(state, llm, result, report, "news",
                                                    task_context=task_context)

        return {
            "messages": [result],
            "news_report": report,
            **checks,
        }

    return news_analyst_node
