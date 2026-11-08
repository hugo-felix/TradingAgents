"""One turn of a tool-using analyst, and its last turn once its tool rounds are spent."""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tradingagents.agents.grounding import (
    evidence_from_state,
    finish_grounded_report,
    grounded_run_enabled,
    grounding_instruction,
    invoke_report_model,
    validation_update,
)

WRAP_UP = (
    "You have used every tool round this report allows. Write your final report "
    "now from the tool results above, and say which data you could not retrieve."
)


def report_instruction(state) -> str:
    """A versioned run separates model interpretation from code-rendered facts."""
    if not grounded_run_enabled(state):
        return '\n Make sure to append a Markdown table at the end of the report to organize key points clearly.'
    return '\n\n' + grounding_instruction(evidence_from_state(state), verify_measurements=True)


def finish_analyst_turn(state, llm, message, report, role, *, task_context=""):
    if message.tool_calls or not grounded_run_enabled(state):
        return message, report, {}
    result = finish_grounded_report(llm, report, evidence_from_state(state), task_context=task_context, verify_measurements=True)
    published = AIMessage(content=result.content, id=message.id)
    return published, result.content, validation_update(role, result)


def take_turn(prompt, llm, tools, messages, grounded=False):
    """The analyst's reply and its report, which stays empty while it calls tools.

    Told to wrap up, it answers without tools: a model offered tools may call one
    whatever it is told. Its earlier calls and their results reach it as text,
    since a provider may refuse a history holding tool calls when none are bound.
    """
    messages = _deduplicate_tool_results(messages)
    if messages and isinstance(messages[-1], HumanMessage) and messages[-1].content == WRAP_UP:
        # The prompt lists the tools it offers; a model told of tools it cannot
        # call may still try, and some providers then answer with nothing.
        prompt = prompt.partial(tool_names="none; your tool rounds are spent")
        if grounded:
            result = invoke_report_model(llm, prompt.invoke(_as_text(messages)).to_messages())
            return result, result.content
        result = (prompt | llm).invoke(_as_text(messages))
        return result, result.content
    result = (prompt | llm.bind_tools(tools)).invoke(messages)
    if grounded and not result.tool_calls:
        # Tool selection can end with prose. Generate the report in schema mode
        # before validation, leaving the single repair available for actual errors.
        prompt = prompt.partial(tool_names="none; tool selection is complete")
        result = invoke_report_model(llm, prompt.invoke(_as_text([*messages, result])).to_messages())
    return result, "" if result.tool_calls else result.content


def _deduplicate_tool_results(messages):
    """Keep repeated payloads once in the prompt; preserve the original evidence history."""
    seen = set()
    written = []
    for message in messages:
        if isinstance(message, ToolMessage) and isinstance(message.content, str):
            key = (message.name, message.content)
            if key in seen:
                message = message.model_copy(update={
                    "content": "Identical result already supplied above by this tool; reuse it."
                })
            seen.add(key)
        written.append(message)
    return written


def _as_text(messages):
    """The history with its tool calls and tool results written out as plain messages."""
    written = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            said = message.content if isinstance(message.content, str) else ""
            calls = "; ".join(f"{c['name']}({json.dumps(c['args'], default=str)})" for c in message.tool_calls)
            written.append(AIMessage(content=f"{said}\n[Called {calls}]".strip()))
        elif isinstance(message, ToolMessage):
            written.append(HumanMessage(content=f"[{message.name or 'Tool'} returned]\n{message.content}"))
        else:
            written.append(message)
    return written
