"""Deterministic reading labels; never translate arbitrary prose or source data."""
from __future__ import annotations

import re

RATING_ZH = {'Buy': '买入', 'Overweight': '增配', 'Hold': '持有', 'Underweight': '减配',
             'Sell': '卖出', 'REVIEW': '需复核'}
ROLE_ZH = {
    'Market Analyst': '技术分析师', 'Sentiment Analyst': '情绪分析师', 'News Analyst': '消息分析师',
    'Fundamentals Analyst': '基本面分析师', 'Bull Researcher': '多方研究员', 'Bear Researcher': '空方研究员',
    'Research Manager': '研究经理', 'Trader': '交易员', 'Aggressive Analyst': '积极风控',
    'Conservative Analyst': '保守风控', 'Neutral Analyst': '中性风控', 'Portfolio Manager': '配置经理',
    'Source facts': '来源事实',
}
SECTION_ZH = {
    'Analyst Team Reports': '各维度分析', 'Research Team Decision': '研究结论',
    'Trading Team Plan': '交易计划', 'Risk Management Team Decision': '风险评估',
    'Portfolio Manager Decision': '最终配置决策', 'Source Facts': '来源事实',
}
LABEL_ZH = {
    'Investment Rating': '评级', 'Rating': '评级', 'Recommendation': '研究建议',
    'Executive Summary': '执行摘要', 'Investment Thesis': '投资论据', 'Rationale': '判断依据',
    'Strategic Actions': '执行策略', 'Action': '交易方向', 'Reasoning': '交易依据',
    'Entry Price': '参考入场价', 'Stop Loss': '止损条件', 'Position Sizing': '仓位建议',
    'Price Target': '目标价', 'Time Horizon': '配置期限', 'Overall Sentiment': '总体情绪',
    'Confidence': '置信程度', 'Score': '情绪评分', 'Analysis date': '分析日期', 'Generated': '生成时间',
    'Trading Analysis Report': '股票研究报告', 'Analysts': '参与分析', 'Data vendors': '数据来源',
    'Memory log': '历史复盘说明',
}
STATUS_ZH = {'available': '已取得', 'unavailable': '不可用', 'missing': '缺失', 'unverified': '未核实',
             'failed': '未通过', 'passed': '已通过', 'skipped': '已跳过', 'error': '异常',
             'empty': '无返回内容', 'withheld': '暂不提供', 'unknown': '未知'}
# Protect identifiers, raw quotations, URLs and code before replacing fixed labels.
_PROTECTED = re.compile(r'(```[\s\S]*?```|`[^`\n]+`|https?://[^\s<>]+|'
                        r'(?m:^>[^\n]*)|[\w.-]+:[\w:.-]+(?:#[\w:.-]+)?|[\w:.-]+#[\w:.-]+)')
_REF = re.compile(r'\s*\[(?:引用|依据|References?)\s*[:：][^\]\n]+\]', re.I)


def chinese_output(settings=None) -> bool:
    # Published artifacts use the run's recorded settings, not another run's
    # process-global locale. Callers without settings retain the original format.
    language = (settings or {}).get('output_language') or 'English'
    return str(language).strip().lower() in {'中文', '简体中文', '繁体中文', 'chinese', 'zh', 'zh-cn', 'zh-tw', 'simplified chinese', 'traditional chinese'}


def localize_report_text(text: str) -> str:
    """Known presentation labels only; numeric values and unknown prose are intact."""
    replacements = {**SECTION_ZH, **ROLE_ZH, **LABEL_ZH}
    def translate(value):
        for source, target in sorted(replacements.items(), key=lambda pair: -len(pair[0])):
            value = re.sub(r'(?<![\w])' + re.escape(source) + r'(?![\w])', target, value)
        # Canonical rating/transaction values are only replaced outside protected data.
        for source, target in RATING_ZH.items():
            value = re.sub(r'(?<![A-Za-z0-9_])' + source + r'(?![A-Za-z0-9_])', target, value, flags=re.I)
        value = re.sub(r'(?m)^(\s*(?:#{1,6}\s*|-\s*)?(?:\*\*)?(?:股票研究报告|分析日期|评级|执行摘要|投资论据|配置期限)(?:\*\*)?)\s*:\s*', r'\1：', value)
        value = value.replace('FINAL TRANSACTION PROPOSAL', '交易方向')
        value = value.replace('No structured source facts available / 无结构化来源事实。', '暂无结构化来源事实。')
        value = value.replace('No structured source facts available', '暂无结构化来源事实')
        value = value.replace('Fact ID', '事实编号').replace('Source ID', '来源编号').replace('Availability', '可用状态')
        value = value.replace('not provided', '未提供').replace('Not provided', '未提供')
        value = value.replace('来源事实与限定条件 / Source facts and qualifications', '来源事实与限定条件')
        value = value.replace('Source fact artifact failed validation; no values published / 来源事实结构无效。', '来源事实结构校验未通过，未发布相关数值。')
        value = value.replace('Source coverage / 来源覆盖（仅工具返回状态，不代表事实全部验证或解析完整）', '来源覆盖（仅表示工具返回状态，不代表事实均已验证或解析完整）')
        value = value.replace('; truncated / 内容截断', '；内容截断')
        value = value.replace('证据不足 / Grounded report validation failed. No validated analytical report available.', '证据不足：报告校验未通过，暂无可用分析结论。')
        value = value.replace('Validation scope: schema and references; prose reasoning is not machine-verified.', '校验范围：字段结构与来源引用；不代表分析推理已经验证。')
        value = value.replace('Validation scope: references, value display and known qualifications; not economic correctness.', '校验范围：来源引用、数值展示与已知限定条件；不代表经济推理正确。')
        value = value.replace('Original source facts remain in evidence and the final report appendix.', '原始来源事实保留在证据详情与报告附录中。')
        # Fixed status cells only; source prose and data identifiers are not relabelled.
        for source, target in STATUS_ZH.items():
            value = re.sub(r'(\|\s*)' + source + r'(\s*(?:;[^|]*)?\|)', lambda m, t=target: m[1] + t + m[2], value)
        return value
    return ''.join(part if i % 2 else translate(part) for i, part in enumerate(_PROTECTED.split(text)))


def reading_text(text: str) -> str:
    """A short reading view; raw source reference strings remain in the original."""
    body, references = split_technical_references(text)
    return localize_report_text(body).strip() or ('相关来源见原文依据。' if references else '')


def split_technical_references(text: str) -> tuple[str, list[str]]:
    """Move explicit reference annotations, not measurements, out of prose."""
    references = []
    def retain(match):
        references.append(match[0].strip())
        return ''
    body = _REF.sub(retain, text)
    body = re.sub(r'(?m)^\s*(?:反向证据|计划依据)[：:]\s*(?:[\w:.-]+#[\w:.-]+(?:[,， ]+|$))+$', retain, body)
    return body, references


def split_source_coverage(text: str) -> tuple[str, str]:
    """Only the writer's coverage appendix is foldable, not an analyst's evidence."""
    marker = re.search(r'(?m)^(?:Source coverage / 来源覆盖|来源覆盖（)[^\n]*', text)
    if marker and '|' in text[marker.end():]:
        return text[:marker.start()].rstrip(), text[marker.start():]
    if re.match(r'^\s*\|\s*(?:Source ID|来源编号)\s*\|\s*(?:Availability|可用状态)\s*\|', text):
        return '', text
    return text, ''
