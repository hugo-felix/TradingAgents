"""Source-linked per-analyst excerpts derived from immutable report snapshots.

No scoring of sentiment, economic assertions, or extra model calls. Explicit
report labels and summary sections determine what can safely be quoted.
"""
from __future__ import annotations

import re

from tradingagents.desktop.reports import plain

ANGLES = (('market', '技术面'), ('fundamentals', '基本面'), ('news', '消息面'), ('sentiment', '情绪面'))
SUMMARY_LABELS = r'执行摘要|核心结论|核心观点|总体结论|综合判断|总体判断|Executive Summary|Key Takeaways|Summary|Conclusion'


def _quote(block, text, kind='analysis'):
    text = re.sub(r'^\s*(?:[-•]\s+|\d+\.\s+|\d+[)）、]\s*)', '', plain(text))
    # Retain the complete labelled passage, including conditions and caveats.
    # The view limits its visual height and links directly to the full source.
    return {'text': text, 'source': block['id'], 'kind': kind}


def _content(text):
    cleaned = plain(text).strip()
    return bool(cleaned) and not re.fullmatch(r'[-_*\s]+', cleaned) and not cleaned.startswith('|')


def _labelled(blocks, labels, kind='analysis'):
    pattern = re.compile(rf'(?:^|\n|[。；])\s*(?:-\s*|\d+[.)、]\s*)?(?:{labels})\s*[：:]\s*([^\n]*)', re.I)
    for index, block in enumerate(blocks):
        text = plain(block['text'])
        match = pattern.search(text)
        if not match:
            continue
        # Markdown soft breaks do not end a paragraph. Include all continuation
        # lines so a subsequent qualification or negation is never discarded.
        passage = text[match.start(1):].strip()
        if passage:
            return _quote(block, passage, kind)
        if index + 1 < len(blocks):
            following = blocks[index + 1]
            if _content(following['text']) and not re.match(r'^(?:#|\*\*)', following['text']):
                return _quote(following, following['text'], kind)
    return None


def _under_heading(blocks, labels, kind='analysis'):
    for index, block in enumerate(blocks):
        first, _, rest = block['text'].partition('\n')
        if not re.match(r'^\s*(?:#{1,6}\s+|\*\*)', first) or not re.search(labels, plain(first), re.I):
            continue
        if _content(rest):
            return _quote(block, rest, kind)
        for following in blocks[index + 1:]:
            if not _content(following['text']):
                continue
            if re.match(r'^(?:#|\*\*)', following['text']):
                break
            return _quote(following, following['text'], kind)
    return None


def _grounded(blocks):
    summary = _labelled(blocks, r'分析推断（非机器证明的经济结论）')
    if not summary:
        return None, None
    limitation = _labelled(blocks, r'证据局限[^：:]*', 'limitation')
    return summary, limitation


def _dimension(key, label, section, validation):
    result = {'key': key, 'label': label, 'status': 'not_run', 'kind': 'analysis', 'items': [], 'source': None}
    blocks = section['blocks'] if section else []
    if not blocks:
        return result
    result['source'] = blocks[0]['id']
    if any(isinstance(validation.get(role), dict) and validation[role].get('status') == 'failed'
           for role in (key, 'social' if key == 'sentiment' else key)):
        result['status'] = 'review'
        return result
    if key == 'sentiment' and any(re.search(
        r'^(?:总体情绪|情绪评级|Overall sentiment)\s*[：:]\s*(?:未评估|无法评估|not assessed)',
        plain(block['text']), re.I | re.M,
    ) for block in blocks):
        limitation = next((b for b in blocks if re.search(r'有限样本|样本.{0,18}(?:有限|不足)|不具代表性', plain(b['text']))), None)
        result['status'] = 'insufficient' if limitation else 'unassessed'
        if limitation:
            result['items'] = [_quote(limitation, limitation['text'], 'limitation')]
        return result
    focus, context = _grounded(blocks)
    if focus is None and key == 'market':
        focus = _labelled(blocks, r'短周期结论|短期结论|技术结论|当前趋势|技术面结论')
        if focus:
            context = _labelled(blocks, r'长期趋势评级|中期趋势评级', 'context')
    if focus is None and key == 'news':
        focus = _labelled(blocks, r'方向判定[^：:]*|新闻结论|消息面结论')
    if focus is None:
        focus = _labelled(blocks, SUMMARY_LABELS) or _under_heading(blocks, SUMMARY_LABELS)
    if focus is None and key == 'fundamentals':
        focus = _labelled(blocks, r'空头/风险逻辑|风险提示|主要风险', 'risk')
        if focus:
            context = _labelled(blocks, r'多头逻辑|支撑因素|正面因素', 'context')
        else:
            focus = _labelled(blocks, r'操作含义')
    if focus is None:
        focus = _under_heading(blocks, r'风险情景|条件情景|Risk Scenario', 'scenario')
    result['status'] = 'ready' if focus else 'unstructured'
    if focus:
        result['kind'] = focus['kind']
        result['items'] = [focus]
        if context and context != focus:
            result['items'].append(context)
    return result


def build_preview(document: dict) -> dict:
    sections = {section['id']: section for section in document.get('sections', [])}
    validation = document.get('validation') or {}
    decision = sections.get('decision', {}).get('blocks', [])
    conclusion = (_labelled(decision, r'执行摘要|Executive Summary')
                  or _under_heading(decision, r'执行摘要|Executive Summary')
                  or document.get('summary', {}).get('conclusion'))
    follow_up = (_labelled(decision, r'会改变本决定的因素|会改变判断的因素|重新评估条件|What would change[^：:]*')
                 or document.get('summary', {}).get('conditions'))
    return {
        'conclusion': conclusion,
        'decision_plan': _decision_plan(decision),
        'dimensions': [_dimension(key, label, sections.get(key), validation) for key, label in ANGLES],
        'follow_up': follow_up,
    }


def _decision_plan(blocks):
    """Quote explicit plan fields; never infer timing or orders from a rating."""
    fields = (
        ('horizon', '配置期限', r'配置期限|持有期限|Time Horizon'),
        ('mode', '执行方式', r'执行方式|Execution Mode'),
        ('execution', '执行条件', r'执行条件|执行计划|Execution Conditions|Execution Plan'),
        ('expiry', '计划有效期', r'计划有效期|执行有效期|Valid Until|Plan Expiry'),
        ('invalidation', '失效条件', r'失效条件|取消条件|Invalidation Conditions'),
        ('catalysts', '事件窗口', r'事件窗口|催化窗口|Event Window'),
        ('counter', '最强反向证据', r'最强反向证据|Strongest Counterevidence'),
        ('benchmark', '市场与板块', r'市场与板块|Benchmark Context'),
    )
    result = []
    for key, label, pattern in fields:
        item = _labelled(blocks, pattern) or _under_heading(blocks, pattern)
        if item is None and key == 'execution':
            block = next((b for b in blocks if re.search(r'执行上\s*[：:]|Execution strategy\s*:', plain(b['text']), re.I)), None)
            # Quote from the explicit execution marker through the end, keeping
            # every stage and defensive exception. Allocation stays in conclusion.
            if block:
                text = plain(block['text'])
                start = re.search(r'执行上\s*[：:]|Execution strategy\s*:', text, re.I).start()
                item = _quote(block, text[start:], 'execution')
        result.append({'key': key, 'label': label, 'item': item})
    return result
