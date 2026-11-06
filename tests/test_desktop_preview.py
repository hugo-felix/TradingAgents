from tests.test_desktop_reports import DECISION, sample_report
from tradingagents.desktop.preview import build_preview
from tradingagents.desktop.reports import build_document, plain
from tradingagents.desktop.store import DesktopStore


def test_preview_shows_four_angles_with_source_links_and_keeps_balanced_context():
    doc = build_document('600941.SS', '2026-11-05', {
        'decision': DECISION,
        'market': '# 技术报告\n\n**短周期结论**：动能已转弱，但不代表长期趋势反转。\n\n**长期趋势评级**：多头，未被本次回调动摇。',
        'fundamentals': '## 执行摘要\n\n盈利承压，但现金流仍有支撑。估值需要继续观察。',
        'news': '## 核心结论\n\n公司是采购方，不能把采购视为销售收入。',
        'sentiment': '**总体情绪：未评估**\n\n讨论仅是有限样本，不代表全体投资者。',
    })
    preview = build_preview(doc)
    assert [item['key'] for item in preview['dimensions']] == ['market', 'fundamentals', 'news', 'sentiment']
    market = preview['dimensions'][0]
    assert '不代表长期趋势反转' in market['items'][0]['text']
    assert any('未被本次回调动摇' in item['text'] for item in market['items'])
    assert preview['dimensions'][3]['status'] == 'insufficient'
    assert preview['follow_up']['source']
    blocks = {b['id']: b for s in doc['sections'] for b in s['blocks']}
    for dimension in preview['dimensions']:
        for item in dimension['items']:
            assert item['text'] in plain(blocks[item['source']]['text'])


def test_grounded_summary_and_limitation_remain_distinct():
    doc = build_document('600941.SS', '2026-11-05', {
        'market': '分析推断（非机器证明的经济结论）：趋势仍需确认。\n\n'
                  '- 分析推断：短期转弱，仍需结合成交量。 [引用：market#close]\n\n'
                  '- 证据局限：无法取得完整量能。 [引用：market#volume]',
    })
    dimension = build_preview(doc)['dimensions'][0]
    assert dimension['status'] == 'ready'
    assert dimension['items'][0]['text'] == '趋势仍需确认。'
    assert dimension['items'][1]['kind'] == 'limitation'
    assert '无法取得' in dimension['items'][1]['text']


def test_missing_failed_and_unstructured_angles_are_not_invented():
    doc = build_document('AAPL', '2026-11-05', {
        'market': '## 数据说明\n\n数据核验完成。',
        'fundamentals': '## 核心结论\n\n经营改善。',
    }, {'report_validation': {'fundamentals': {'status': 'failed'}}})
    angles = {item['key']: item for item in build_preview(doc)['dimensions']}
    assert angles['market']['status'] == 'unstructured'
    assert angles['market']['items'] == []
    assert angles['news']['status'] == 'not_run'
    assert angles['fundamentals']['status'] == 'review'
    assert angles['fundamentals']['items'] == []


def test_conditional_risk_is_never_rendered_as_an_observed_fact():
    doc = build_document('AAPL', '2026-11-05', {
        'market': '## 风险情景\n\n若支撑失守，回调可能升级。',
    })
    angle = build_preview(doc)['dimensions'][0]
    assert angle['kind'] == 'scenario'
    assert angle['items'][0]['text'].startswith('若支撑失守')


def test_legacy_report_gains_preview_without_reimport_or_new_history(tmp_path):
    source = sample_report(tmp_path / 'source')
    store = DesktopStore(tmp_path / 'workspace')
    store.import_directory(source)
    report_id = store.list_reports()[0]['id']
    with store.connect() as db:
        original = db.execute('SELECT document FROM reports WHERE id=?', (report_id,)).fetchone()[0]
    report = store.get_report(report_id)
    assert len(report['preview']['dimensions']) == 4
    with store.connect() as db:
        assert db.execute('SELECT document FROM reports WHERE id=?', (report_id,)).fetchone()[0] == original
    assert store.import_directory(source)['added'] == 0


def test_lightweight_preview_payload_excludes_full_report_body(tmp_path):
    from tradingagents.desktop.api import DesktopAPI

    source = sample_report(tmp_path / 'source')
    store = DesktopStore(tmp_path / 'workspace')
    store.import_directory(source)
    report_id = store.list_reports()[0]['id']
    response = DesktopAPI(store).get_preview(report_id)
    assert response['ok']
    assert 'sections' not in response['data']
    assert 'original_markdown' not in response['data']
    assert len(response['data']['preview']['dimensions']) == 4


def test_late_qualifications_in_an_explicit_summary_are_kept_in_full():
    doc = build_document('AAPL', '2026-11-05', {
        'market': '## 核心结论\n\n短期转弱。波动加大。但这些信号不能证明长期趋势已经反转。',
    })
    item = build_preview(doc)['dimensions'][0]['items'][0]
    assert item['text'].endswith('不能证明长期趋势已经反转。')


def test_soft_wrapped_qualifications_stay_in_the_same_quoted_passage():
    doc = build_document('AAPL', '2026-11-05', {
        'market': '**短周期结论**：短期转弱。\n但不代表长期趋势反转。',
    })
    item = build_preview(doc)['dimensions'][0]['items'][0]
    assert '但不代表长期趋势反转。' in item['text']


def test_label_and_body_on_separate_lines_are_still_one_passage():
    doc = build_document('AAPL', '2026-11-05', {
        'market': '**短周期结论**：\n短期转弱，但长期趋势未破坏。',
    })
    item = build_preview(doc)['dimensions'][0]['items'][0]
    assert item['text'] == '短期转弱，但长期趋势未破坏。'


def test_overall_conclusion_and_follow_up_keep_their_full_qualifications():
    doc = build_document('AAPL', '2026-11-05', {
        'decision': '**Rating**: Hold\n\n**执行摘要**：建议观察。\n但结论依赖未核实数据。\n\n'
                    '**会改变本决定的因素**：若现金流改善可重估。\n但仍须核查披露口径。',
    })
    preview = build_preview(doc)
    assert '但结论依赖未核实数据' in preview['conclusion']['text']
    assert '但仍须核查披露口径' in preview['follow_up']['text']


def test_decimal_value_at_start_of_quote_is_not_mistaken_for_a_list_marker():
    doc = build_document('AAPL', '2026-11-05', {
        'market': '**核心结论**：1.8% 的波动并不构成反转证据。',
    })
    item = build_preview(doc)['dimensions'][0]['items'][0]
    assert item['text'] == '1.8% 的波动并不构成反转证据。'


def test_preview_separates_new_plan_fields_and_keeps_source_links():
    doc = build_document('600941.SS', '2026-11-05', {
        'decision': '**Rating**: Underweight\n\n**执行摘要**：配置偏谨慎。\n\n'
                    '**配置期限**：一个季度。\n\n**执行方式**：条件触发\n\n'
                    '**执行条件**：反弹至101.2、103.7分批减持；跌破98.80才执行防守。\n\n'
                    '**计划有效期**：下次披露时重审。\n\n**失效条件**：趋势或经营证据改变。\n\n'
                    '**事件窗口**：大会正在举行，需跟踪新信息。',
    })
    fields = {item['key']: item for item in build_preview(doc)['decision_plan']}
    assert '101.2、103.7' in fields['execution']['item']['text']
    assert '跌破98.80才' in fields['execution']['item']['text']
    assert fields['horizon']['item']['text'] == '一个季度。'
    assert fields['expiry']['item'] and fields['invalidation']['item'] and fields['catalysts']['item']
    ids = {b['id'] for section in doc['sections'] for b in section['blocks']}
    assert all(field['item']['source'] in ids for field in fields.values() if field['item'])


def test_preview_legacy_execution_keeps_entire_conditional_passage_without_inventing_horizon():
    text = '**执行摘要**\n\n减配，非清仓。执行上：第一批101.2限价，第二批103.7；若跌破98.80才转市价。'
    doc = build_document('600941.SS', '2026-11-05', {'decision': text})
    fields = {item['key']: item for item in build_preview(doc)['decision_plan']}
    assert '减配，非清仓' in build_preview(doc)['conclusion']['text']
    assert fields['execution']['item']['text'].startswith('执行上：')
    assert '若跌破98.80才转市价' in fields['execution']['item']['text']
    assert fields['horizon']['item'] is None and fields['expiry']['item'] is None
    assert '\n\n'.join(b['text'] for b in doc['sections'][0]['blocks']) == text
