"""Chinese reading and publication must not mutate raw evidence or protocol values."""
import json

import pytest

from tests.test_grounding import Model, evidence, payload
from tradingagents.agents.context import get_language_instruction
from tradingagents.agents.grounding import finish_grounded_report
from tradingagents.agents.rating import extract_rating
from tradingagents.dataflows.config import run_config
from tradingagents.desktop.store import DesktopStore
from tradingagents.reporting import write_report_tree


def test_chinese_generation_explains_structured_field_language_and_terminology():
    with run_config({'output_language': '简体中文'}):
        instruction = get_language_instruction()
    assert 'JSON keys' in instruction and 'natural-language field values' in instruction
    assert 'first occurrence' in instruction and 'source IDs' in instruction


def test_grounded_repair_retains_chinese_language_requirement():
    llm = Model([json.dumps(payload())])
    with run_config({'output_language': '简体中文'}):
        result = finish_grounded_report(llm, 'bad', evidence())
    assert result.status == 'passed'
    assert '简体中文' in llm.calls[0] and 'natural-language field values' in llm.calls[0]


@pytest.mark.parametrize('text,expected', [
    ('**评级**：减配\n\n不建议卖出。', 'Underweight'),
    ('**评级**：买入\n\n暂不采纳减配意见。', 'Buy'),
    ('不建议卖出，应观察。', None),
    ('- 评级：买入\n- 评级：卖出', None),
    ('**Rating**: Hold\n\n评级：卖出', 'Hold'),
])
def test_explicit_chinese_rating_keeps_negation_and_conflict_rules(text, expected):
    assert extract_rating(text) == expected


def report_state():
    return {'trade_date': '2026-11-05', 'final_rating': 'Underweight',
            'final_trade_decision': '**Rating**: Underweight\n\n**Executive Summary**: 反弹分批减配，非清仓。\n\n'
                                    '**Time Horizon**: 一个季度。',
            'market_report': '分析推断（非机器证明的经济结论）：短期动能转弱。\n\n'
                             '- 分析推断：保持观察。 [引用：market:snapshot:1#close]',
            'report_validation': {'portfolio': {'status': 'passed'}}}


def test_chinese_publication_localizes_markdown_html_and_remains_importable(tmp_path):
    with run_config({'output_language': '简体中文'}):
        output = write_report_tree(report_state(), '600941.SS', tmp_path,
                                   settings={'output_language': '简体中文', 'analysts': ['market']})
    text = output.read_text(encoding='utf-8')
    html = output.with_suffix('.html').read_text(encoding='utf-8')
    decision = (tmp_path / '5_portfolio/decision.md').read_text(encoding='utf-8')
    assert '# 股票研究报告：600941.SS' in text
    assert '- 分析日期：2026-11-05' in text
    assert '**评级**：减配' in decision and '**执行摘要**' in decision
    assert 'Portfolio Manager Decision' not in text and 'Fact contract checks' not in text
    assert '<html lang="zh-CN">' in html and '分析日期' in html
    assert 'market:snapshot:1#close' in text
    store = DesktopStore(tmp_path / 'app')
    assert store.import_directory(tmp_path)['added'] == 1
    saved = store.list_reports()[0]
    assert saved['ticker'] == '600941.SS' and saved['date'] == '2026-11-05'
    assert saved['rating'] == 'Underweight'


def test_english_publication_keeps_existing_contract(tmp_path):
    with run_config({'output_language': 'English'}):
        output = write_report_tree(report_state(), 'AAPL', tmp_path,
                                   settings={'output_language': 'English'})
    assert '# Trading Analysis Report: AAPL' in output.read_text(encoding='utf-8')
    assert '**Rating**: Underweight' in (tmp_path / '5_portfolio/decision.md').read_text(encoding='utf-8')
    assert '<html lang="en">' in output.with_suffix('.html').read_text(encoding='utf-8')


def test_chinese_standalone_report_import_without_state_or_metadata(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'complete_report.md').write_text(
        '# 股票研究报告：600941.SS\n\n- 分析日期：2026-11-05\n- 评级：减配\n\n'
        '## V. 最终配置决策\n\n### 配置经理\n\n**评级**：减配\n\n**执行摘要**：条件触发。', encoding='utf-8')
    store = DesktopStore(tmp_path / 'app')
    assert store.import_directory(source)['added'] == 1
    report = store.get_report(store.list_reports()[0]['id'])
    assert report['rating'] == 'Underweight' and report['date'] == '2026-11-05'
    assert report['preview']['conclusion']['text'] == '条件触发。'


def test_reading_fields_hide_machine_references_and_keep_immutable_raw_snapshot(tmp_path):
    from tests.test_desktop_reports import sample_report
    text = '**Rating**: Underweight\n\n**执行摘要**：维持 Underweight，非 Sell。 [引用：market:snapshot:1#close]\n\n'
    source = sample_report(tmp_path / 'source', decision=text)
    store = DesktopStore(tmp_path / 'app')
    store.import_directory(source)
    rid = store.list_reports()[0]['id']
    with store.connect() as db:
        before = db.execute('SELECT document FROM reports WHERE id=?', (rid,)).fetchone()[0]
    doc = store.get_report(rid)
    item = doc['preview']['conclusion']
    assert item['display_text'] == '维持 减配，非 卖出。'
    assert 'market:snapshot:1#close' in item['text']
    block = next(b for s in doc['sections'] for b in s['blocks'] if b['id'] == item['source'])
    assert '<details' in block['display_html'] and '技术引用' in block['display_html']
    assert 'market:snapshot:1#close' in block['display_html']
    assert '**Rating**: Underweight' in doc['original_markdown']
    with store.connect() as db:
        assert db.execute('SELECT document FROM reports WHERE id=?', (rid,)).fetchone()[0] == before


def test_reading_renderer_preserves_url_ticker_numbers_and_rejects_active_markup():
    from tradingagents.desktop.reports import render_reading_markdown
    raw = '**Rating**: Underweight\n\n101.2 / 98.80，600941.SS，FCF。 [引用：market:snapshot:1#close]\n\n<script>alert(1)</script>'
    html = render_reading_markdown(raw)
    assert '减配' in html and '101.2 / 98.80' in html and '600941.SS' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert 'market:snapshot:1#close' in html and '<details' in html


def test_fixed_translation_does_not_change_url_or_source_identifier_or_code():
    from tradingagents.report_language import localize_report_text
    raw = '**Rating**: Underweight\n`Underweight` https://example.com/Underweight filing#Hold\n> Original Sell quote'
    translated = localize_report_text(raw)
    assert '**评级**：减配' in translated
    for value in ['`Underweight`', 'https://example.com/Underweight', 'filing#Hold', '> Original Sell quote']:
        assert value in translated


def test_chinese_html_citations_are_collapsed_but_source_data_remains_present(tmp_path):
    output = write_report_tree(report_state(), '600941.SS', tmp_path,
                               settings={'output_language': '简体中文'})
    html = output.with_suffix('.html').read_text(encoding='utf-8')
    assert '<details class="technical-references">' in html
    assert '技术引用' in html and 'market:snapshot:1#close' in html


def test_source_coverage_table_is_advanced_detail_in_chinese_reading_and_html(tmp_path):
    from tradingagents.desktop.reports import render_reading_markdown
    table = '| Source ID | Availability |\n| --- | --- |\n| market:get_stock_data:1 | available |'
    assert '<details class="source-coverage">' in render_reading_markdown(table)
    state = report_state()
    state['market_report'] += '\n\nSource coverage / 来源覆盖（仅工具返回状态，不代表事实全部验证或解析完整）\n' + table
    output = write_report_tree(state, '600941.SS', tmp_path, settings={'output_language': '简体中文'})
    html = output.with_suffix('.html').read_text(encoding='utf-8')
    assert '<details class="source-coverage">' in html
    assert 'market:get_stock_data:1' in html
