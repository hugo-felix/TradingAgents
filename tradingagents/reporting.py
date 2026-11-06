"""Reusable report-tree writer shared by the CLI and the programmatic API.

Writes a run's per-section markdown (analysts, research, trading, risk,
portfolio) plus a consolidated ``complete_report.md`` under ``save_path``. The
CLI and ``TradingAgentsGraph.save_reports`` both call this, so a headless / API
run produces the same on-disk report tree a CLI run does.
"""

from datetime import datetime
from pathlib import Path

from tradingagents.agents.grounding import (
    evidence_from_state,
    facts_from_evidence,
    grounded_run_enabled,
)
from tradingagents.agents.rating import run_rating
from tradingagents.dataflows.facts import render_facts
from tradingagents.dataflows.symbols import is_a_share
from tradingagents.report_language import RATING_ZH, chinese_output, localize_report_text


def analyst_names(keys) -> list[str]:
    """The analysts as users select them: the sentiment analyst's key is "social"."""
    return ["sentiment" if key == "social" else key for key in keys or []]


def _header(ticker: str, final_state: dict, settings: dict | None) -> str:
    """The report's title and what produced it: analysis date, version, models, analysts, vendors."""
    if chinese_output(settings):
        return _chinese_header(ticker, final_state, settings)
    lines = [f"# Trading Analysis Report: {ticker}", ""]
    if final_state.get("trade_date"):
        lines.append(f"- Analysis date: {final_state['trade_date']}")
    if final_state.get("final_trade_decision"):
        lines.append(f"- Rating: {run_rating(final_state)}")
    lines.append(f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if settings:
        s = settings.get
        deep_provider = s("deep_think_provider") or s("llm_provider", "?")
        quick_provider = s("quick_think_provider") or s("llm_provider", "?")
        if deep_provider == quick_provider:
            models = f"{deep_provider}, deep {s('deep_think_llm', '?')}, quick {s('quick_think_llm', '?')}"
        else:   # each tier on its own provider (#1440)
            models = (f"deep {deep_provider} {s('deep_think_llm', '?')}, "
                      f"quick {quick_provider} {s('quick_think_llm', '?')}")
        lines.append(f"- TradingAgents {s('version', '?')}: {models}")
        lines.append(f"- Analysts: {', '.join(analyst_names(s('analysts')))}; "
                     f"research debate rounds {s('max_debate_rounds', '?')}, "
                     f"risk debate rounds {s('max_risk_discuss_rounds', '?')}")
        vendors = {**(s("data_vendors") or {}), **(s("tool_vendors") or {})}
        if vendors:
            lines.append("- Data vendors: " + ", ".join(f"{k} {v}" for k, v in vendors.items()))
    if final_state.get("memory_note"):
        lines.append(f"- Memory log: {final_state['memory_note']}")
    if final_state.get('report_validation'):
        checks = final_state['report_validation']
        failed = sum(entry.get('status') == 'failed' for entry in checks.values())
        passed = sum(entry.get('status') == 'passed' for entry in checks.values())
        skipped = sum(entry.get('status') == 'skipped' for entry in checks.values())
        lines.append(f'- Fact contract checks: {passed} passed, {failed} failed, {skipped} skipped; references/value display/known qualifications, not economic correctness.')
    return "\n".join(lines) + "\n\n"


def _chinese_header(ticker, final_state, settings):
    lines = [f'# 股票研究报告：{ticker}', '']
    if final_state.get('trade_date'):
        lines.append(f"- 分析日期：{final_state['trade_date']}")
    if final_state.get('final_trade_decision'):
        lines.append('- 评级：' + RATING_ZH.get(run_rating(final_state), '需复核'))
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    checks = final_state.get('report_validation') or {}
    if checks:
        passed = sum(row.get('status') == 'passed' for row in checks.values())
        failed = sum(row.get('status') == 'failed' for row in checks.values())
        skipped = sum(row.get('status') == 'skipped' for row in checks.values())
        lines.append(f'- 报告校验：通过 {passed} 项，未通过 {failed} 项，跳过 {skipped} 项；校验不代表经济推理正确。')
    if settings:
        s = settings.get
        roles = {'market': '技术面', 'social': '情绪面', 'sentiment': '情绪面', 'fundamentals': '基本面', 'news': '消息面'}
        lines.extend(['', '<details><summary>生成与数据来源</summary>', '',
                      '- 参与分析：' + '、'.join(roles.get(key, key) for key in s('analysts', [])),
                      f"- 研究辩论：{s('max_debate_rounds', '?')} 轮；风险辩论：{s('max_risk_discuss_rounds', '?')} 轮"])
        for tier, label in (('deep', '深入分析模型'), ('quick', '快速分析模型')):
            provider = s(f'{tier}_think_provider') or s('llm_provider', '?')
            lines.append(f"- {label}：{provider} / {s(f'{tier}_think_llm', '?')}")
        vendors = {**(s('data_vendors') or {}), **(s('tool_vendors') or {})}
        if vendors:
            lines.append('- 数据来源：' + '、'.join(dict.fromkeys(str(value) for value in vendors.values())))
        lines.extend([f"- 引擎版本：TradingAgents {s('version', '?')}", '', '</details>'])
    if final_state.get('memory_note'):
        lines.append('- 历史复盘说明：' + final_state['memory_note'])
    return '\n'.join(lines) + '\n\n'


# The report's sections in order: (heading, folder, [(agent, file, state path)]).
_SECTIONS = (
    ("I. Analyst Team Reports", "1_analysts", (
        ("Market Analyst", "market.md", ("market_report",)),
        ("Sentiment Analyst", "sentiment.md", ("sentiment_report",)),
        ("News Analyst", "news.md", ("news_report",)),
        ("Fundamentals Analyst", "fundamentals.md", ("fundamentals_report",)),
    )),
    ("II. Research Team Decision", "2_research", (
        ("Bull Researcher", "bull.md", ("investment_debate_state", "bull_history")),
        ("Bear Researcher", "bear.md", ("investment_debate_state", "bear_history")),
        ("Research Manager", "manager.md", ("investment_plan",)),
    )),
    ("III. Trading Team Plan", "3_trading", (
        ("Trader", "trader.md", ("trader_investment_plan",)),
    )),
    ("IV. Risk Management Team Decision", "4_risk", (
        ("Aggressive Analyst", "aggressive.md", ("risk_debate_state", "aggressive_history")),
        ("Conservative Analyst", "conservative.md", ("risk_debate_state", "conservative_history")),
        ("Neutral Analyst", "neutral.md", ("risk_debate_state", "neutral_history")),
    )),
    ("V. Portfolio Manager Decision", "5_portfolio", (
        ("Portfolio Manager", "decision.md", ("final_trade_decision",)),
    )),
)


def _report_parts(final_state: dict) -> list[tuple[str, str, list[tuple[str, str, str]]]]:
    """The sections with something to say: (heading, folder, [(agent, file, text)])."""
    parts = []
    for heading, folder, agents in _SECTIONS:
        written = []
        for agent, filename, path in agents:
            value = final_state
            for key in path:
                value = (value or {}).get(key)
            if value:
                written.append((agent, filename, value))
        if written:
            parts.append((heading, folder, written))
    if grounded_run_enabled(final_state):
        try:
            facts = facts_from_evidence(evidence_from_state(final_state))
            appendix = render_facts(facts)
        except ValueError:
            if run_rating(final_state) != 'REVIEW':
                raise ValueError('Invalid source facts require REVIEW before publication') from None
            appendix = 'Source fact artifact failed validation; no values published / 来源事实结构无效。'
        if appendix:
            text = ('来源事实与限定条件 / Source facts and qualifications\n\n'
                    '以下为来源记录，不代表已验证经济推理；各章节通过 Fact ID 引用。\n\n' + appendix)
            parts.append(('VI. Source Facts', '6_evidence', [('Source facts', 'facts.md', text)]))
    return parts


def write_report_tree(final_state: dict, ticker: str, save_path, settings: dict | None = None,
                      html: bool = True) -> Path:
    """Save a completed run's reports to ``save_path``; return the complete-report path.

    ``settings`` (``TradingAgentsGraph.run_settings()``) adds what produced the run
    to the report's header. ``html`` also writes ``complete_report.html``, one
    self-contained page of the same report (#1419).
    Reusing a directory replaces the generated report files, removing sections
    absent from this run and disabled HTML. Other files and directories are kept.
    """
    if is_a_share(ticker) and any(entry.get('status') == 'failed'
                                 for entry in final_state.get('report_validation', {}).values()) and run_rating(final_state) != 'REVIEW':
        raise ValueError('Failed report validation requires REVIEW before publication')
    parts = _report_parts(final_state)
    if chinese_output(settings):
        parts = [(localize_report_text(heading), folder,
                  [(localize_report_text(agent), filename, localize_report_text(text))
                   for agent, filename, text in written]) for heading, folder, written in parts]
    save_path = Path(save_path)
    save_path.mkdir(parents=True, exist_ok=True)
    sections = []
    for heading, folder, written in parts:
        (save_path / folder).mkdir(exist_ok=True)
        for _agent, filename, text in written:
            (save_path / folder / filename).write_text(text, encoding="utf-8")
        content = "\n\n".join(f"### {agent}\n{text}" for agent, _filename, text in written)
        sections.append(f"## {heading}\n\n{content}")

    (save_path / "complete_report.md").write_text(
        _header(ticker, final_state, settings) + "\n\n".join(sections), encoding="utf-8"
    )
    if html:
        from tradingagents.report_html import render_report

        (save_path / "complete_report.html").write_text(
            render_report(ticker, final_state, settings, parts), encoding="utf-8"
        )
    else:
        (save_path / "complete_report.html").unlink(missing_ok=True)

    # Reconcile only the filenames owned by this writer; never clear the tree,
    # since callers may keep notes or other artifacts alongside the reports.
    current_files = {
        (folder, filename)
        for _heading, folder, written in parts
        for _agent, filename, _text in written
    }
    for _heading, folder, agents in _SECTIONS:
        for _agent, filename, _path in agents:
            if (folder, filename) not in current_files:
                (save_path / folder / filename).unlink(missing_ok=True)
    if ('6_evidence', 'facts.md') not in current_files:
        (save_path / '6_evidence' / 'facts.md').unlink(missing_ok=True)
    return save_path / "complete_report.md"
