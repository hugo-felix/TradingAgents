"""Lossless report snapshots and source-linked excerpts; never another LLM call."""

from __future__ import annotations

import re
from html import escape, unescape

from markdown_it import MarkdownIt

from tradingagents.agents.rating import extract_rating
from tradingagents.report_language import (
    localize_report_text,
    reading_text,
    split_source_coverage,
    split_technical_references,
)

SECTION_FILES = (
    ('decision', '最新模型观点', '5_portfolio/decision.md'),
    ('facts', '来源事实与口径', '6_evidence/facts.md'),
    ('fundamentals', '基本面', '1_analysts/fundamentals.md'),
    ('market', '技术与行情', '1_analysts/market.md'),
    ('news', '新闻与宏观', '1_analysts/news.md'),
    ('sentiment', '市场情绪', '1_analysts/sentiment.md'),
    ('research', '研究结论', '2_research/manager.md'),
    ('trader', '交易计划', '3_trading/trader.md'),
    ('bull', '多方论证', '2_research/bull.md'),
    ('bear', '空方论证', '2_research/bear.md'),
    ('aggressive', '积极风控', '4_risk/aggressive.md'),
    ('conservative', '保守风控', '4_risk/conservative.md'),
    ('neutral', '中性风控', '4_risk/neutral.md'),
)
STATE_FIELDS = {
    'decision': 'final_trade_decision', 'fundamentals': 'fundamentals_report',
    'market': 'market_report', 'news': 'news_report', 'sentiment': 'sentiment_report',
    'research': 'investment_plan', 'trader': 'trader_investment_plan',
}
_LABEL = re.compile(r'^(?:#{1,6}\s+(.+)|\*\*([^*\n]+)\*\*\s*[:：]?\s*(.*))$', re.M)


def plain(text: str) -> str:
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.M)
    return unescape(re.sub(r'[*`]', '', text)).strip()


def render_markdown(text: str) -> str:
    """Disable raw HTML, images, and active links in untrusted model content."""
    md = MarkdownIt('commonmark', {'html': False, 'linkify': False}).enable('table')
    md.renderer.rules['image'] = lambda tokens, idx, *_: escape(tokens[idx].content)
    md.renderer.rules['link_open'] = lambda *_: '<span class="source-link">'
    md.renderer.rules['link_close'] = lambda *_: '</span>'
    return md.render(text)


def render_reading_markdown(text: str) -> str:
    """Known Chinese labels with raw machine citations in an opt-in detail."""
    body, coverage = split_source_coverage(text)
    body, references = split_technical_references(body)
    result = render_markdown(localize_report_text(body))
    if coverage:
        result += '<details class="source-coverage"><summary>来源覆盖详情</summary>' + render_markdown(localize_report_text(coverage)) + '</details>'
    if references:
        result += '<details class="technical-references"><summary>技术引用</summary><pre>' + escape('\n'.join(references)) + '</pre></details>'
    return result


def prepare_reading_document(document):
    """Add transient display fields; never overwrite the stored raw snapshot."""
    for section in document.get('sections', []):
        for block in section.get('blocks', []):
            block['display_html'] = render_reading_markdown(block['text'])
    def decorate(value):
        if isinstance(value, dict):
            if isinstance(value.get('text'), str) and value.get('source'):
                value['display_text'] = reading_text(value['text'])
            for child in list(value.values()):
                if isinstance(child, (dict, list)):
                    decorate(child)
        elif isinstance(value, list):
            for child in value:
                decorate(child)
    decorate(document.get('summary', {}))
    decorate(document.get('preview', {}))
    for change in document.get('changes', []):
        change['display_before'] = reading_text(change.get('before', ''))
        change['display_after'] = reading_text(change.get('after', ''))
    return document


def export_markdown(document: dict) -> str:
    """Keep the original intact while preserving the snapshot's effective status."""
    preface = [f"# StockSeek研究快照 · {document['ticker']} · {document['date']}",
               f"\n有效状态：{document['rating']}；原文模型评级：{document['model_rating']}"]
    preface.extend('\n- ' + warning for warning in document['warnings'])
    return '\n'.join(preface) + '\n\n---\n\n' + document['original_markdown']


def _blocks(key: str, text: str) -> list[dict]:
    # Individual numbered arguments need stable destinations even without blank lines.
    paragraphs = re.split(r'\n\s*\n|\n(?=\d+[.)、]\s)|\n(?=- (?:分析推断|证据局限|条件情景))', text.strip())
    return [
        {'id': f'{key}-{i}', 'text': value.strip(), 'html': render_markdown(value.strip())}
        for i, value in enumerate(paragraphs) if value.strip()
    ]


def _excerpt(block: dict, text: str) -> dict:
    text = plain(text)
    # Keep a complete sentence, including qualifiers. The UI clamps long excerpts
    # visually and always offers the full original; never synthesize a new claim.
    sentence = re.split(r'(?<=[。！？])\s*|(?<=[.!?])\s+(?=[A-Z])', text)[0]
    return {'text': sentence, 'source': block['id']}


def _summary(blocks: list[dict]) -> dict:
    result = {'conclusion': None, 'reasons': [], 'counter': None, 'conditions': None}
    if any('分析推断（非机器证明的经济结论）：' in b['text'] for b in blocks):
        for block in blocks:
            text = block['text']
            summary = re.search(r'^分析推断（非机器证明的经济结论）：(.+)', text, re.M)
            if summary:
                result['conclusion'] = _excerpt(block, summary[1])
            analysis = re.search(r'^- 分析推断[^：]*：(.+)', text, re.M)
            if analysis and len(result['reasons']) < 3:
                result['reasons'].append(_excerpt(block, analysis[1]))
            scenario = re.search(r'^- 条件情景[^：]*：(.+)', text, re.M)
            if scenario and result['conditions'] is None:
                result['conditions'] = _excerpt(block, scenario[1])
            counter = re.search(r'反向证据：([^\n]+)', text)
            limitation = re.search(r'^- 证据局限[^：]*：(.+)', text, re.M)
            if counter and result['counter'] is None:
                result['counter'] = _excerpt(block, counter[0])
            elif limitation and result['counter'] is None:
                result['counter'] = _excerpt(block, limitation[1])
        return result
    current = None
    for block in blocks:
        text = block['text']
        match = _LABEL.match(text)
        if match:
            label = plain(match[1] or match[2]).strip(':： ').lower()
            body = match[3] if match[2] else ''
            if re.search(r'执行摘要|executive summary', label):
                current = 'conclusion'
            elif re.search(r'投资论据|investment thesis|投资逻辑', label):
                current = 'reasons'
            elif re.search(r'改变.*(决定|判断)|change.*(decision|view)|重新评估条件', label):
                current = 'conditions'
            elif re.search(r'为何不|反向证据|counter|风险|risk', label):
                current = 'counter'
            else:
                current = None
            text = body.strip()
        if not text or not current:
            continue
        item = _excerpt(block, text)
        if current == 'reasons':
            if not re.match(r'^\d+[.)、]\s', text) and plain(text).endswith((':', '：')):
                continue
            if len(result['reasons']) < 3:
                result['reasons'].append(item)
        elif result[current] is None:
            result[current] = item
    return result


def build_document(ticker: str, date: str, texts: dict, state: dict | None = None) -> dict:
    state = state or {}
    labels = {key: title for key, title, _ in SECTION_FILES}
    sections = [
        {'id': key, 'title': labels.get(key, '完整研究'), 'debate': key in {
            'bull', 'bear', 'aggressive', 'conservative', 'neutral',
        }, 'blocks': _blocks(key, text)}
        for key, text in texts.items() if isinstance(text, str) and text.strip()
    ]
    model_rating = extract_rating(texts.get('decision', '')) or 'REVIEW'
    validation = state.get('report_validation') or {}
    failed = any(isinstance(v, dict) and v.get('status') == 'failed' for v in validation.values())
    review_required = failed or state.get('final_rating') == 'REVIEW'
    warnings = []
    if failed:
        warnings.append('报告事实校验未通过，需复核；原模型评级仅供追溯。')
    elif review_required:
        warnings.append('原始报告已标记为需复核，正文中的模型评级不作为可用结论。')
    if not validation:
        warnings.append('这份历史报告没有事实校验记录，不能视为已通过校验。')
    decision_blocks = next((s['blocks'] for s in sections if s['id'] == 'decision'), [])
    summary = _summary(decision_blocks)
    if not summary['conclusion']:
        warnings.append('摘要未结构化，请查看最终决策原文。')
    coverage = []
    for field in ('market_evidence', 'fundamentals_evidence', 'news_evidence', 'sentiment_evidence'):
        evidence = state.get(field) or {}
        if not isinstance(evidence, dict):
            continue
        for ref, item in evidence.items():
            if not isinstance(item, dict):
                continue
            status = str(item.get('status', 'unknown'))
            content = item.get('content', '')
            content = content if isinstance(content, str) else ''
            coverage.append({'reference': str(ref), 'tool': str(item.get('tool', ref)),
                             'status': status, 'html': render_markdown(content)})
    unavailable = sum(item['status'] in {'unavailable', 'failed', 'error'} for item in coverage)
    if unavailable:
        warnings.append(f'{unavailable} 项来源不可用，相关判断存在数据缺口。')
    name_match = re.search(re.escape(ticker) + r'[（(]([^，,）)]+)', texts.get('decision', ''))
    return {
        'schema_version': 1, 'ticker': ticker, 'date': date,
        'name': name_match[1] if name_match else ticker,
        'rating': 'REVIEW' if review_required else (state.get('final_rating') or model_rating), 'model_rating': model_rating,
        'summary': summary, 'sections': sections, 'warnings': warnings,
        'coverage': coverage, 'validation': validation,
        'character_count': sum(len(text) for text in texts.values()),
    }
