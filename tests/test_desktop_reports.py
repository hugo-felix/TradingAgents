import json

import pytest

from tradingagents.desktop.reports import build_document, plain, render_markdown
from tradingagents.desktop.store import DesktopStore

DECISION = '''**Rating**: Underweight

**执行摘要**

盈利承压，但现金流构成缓冲，建议降低配置而非清仓。

**投资论据**

决定本案的证据链：

1. **盈利承压，但不是偿付风险。** 利润同比下降，经营现金流仍增长。
2. **估值偏高。** 需要结合盈利趋势观察。
3. **分红增长放缓。** 历史分红不能代表未来。
4. **技术面走弱。** 波动上升。

**为何不升级至Sell（清仓）**：现金流增长，低负债构成反向证据。

**会改变本决定的因素**：盈利与现金流改善后重新评估。
'''


def sample_report(directory, decision=DECISION, date='2026-11-05'):
    directory.mkdir(parents=True)
    (directory / '5_portfolio').mkdir()
    (directory / '5_portfolio/decision.md').write_text(decision, encoding='utf-8')
    (directory / 'complete_report.md').write_text(
        f'# Trading Analysis Report: 600941.SS\n\n- Analysis date: {date}\n\n'
        f'## V. Portfolio Manager Decision\n\n### Portfolio Manager\n\n{decision}',
        encoding='utf-8',
    )
    return directory


def test_summary_preserves_qualifications_and_links_to_full_source():
    doc = build_document('600941.SS', '2026-11-05', {'decision': DECISION})
    assert doc['rating'] == 'Underweight'
    assert doc['summary']['conclusion']['text'] == '盈利承压，但现金流构成缓冲，建议降低配置而非清仓。'
    assert len(doc['summary']['reasons']) == 3
    assert '但不是偿付风险' in doc['summary']['reasons'][0]['text']
    assert '现金流增长' in doc['summary']['counter']['text']
    assert '重新评估' in doc['summary']['conditions']['text']
    blocks = {b['id']: b for s in doc['sections'] for b in s['blocks']}
    for item in [doc['summary']['conclusion'], *doc['summary']['reasons'],
                 doc['summary']['counter'], doc['summary']['conditions']]:
        assert item['text'] in plain(blocks[item['source']]['text'])


def test_missing_summary_is_not_invented_and_failed_checks_force_review():
    doc = build_document('AAPL', '2026-11-05', {'decision': '**Rating**: Buy\n\n长正文'},
                         {'report_validation': {'fundamentals': {'status': 'failed'}}})
    assert doc['rating'] == 'REVIEW'
    assert doc['model_rating'] == 'Buy'
    assert doc['summary']['conclusion'] is None
    assert doc['warnings']


def test_markdown_cannot_run_script_or_load_remote_image():
    html = render_markdown('<script>alert(1)</script>\n\n![x](https://evil.test/x)\n\n[x](javascript:alert(1))')
    assert '<script>' not in html
    assert '<img' not in html
    assert 'href="javascript:' not in html
    assert '&lt;script&gt;' in html


def test_import_is_snapshot_deduplicated_and_compares_same_day_versions(tmp_path):
    source = sample_report(tmp_path / 'source')
    store = DesktopStore(tmp_path / 'app')
    first = store.import_directory(source)
    assert first['added'] == 1
    assert store.import_directory(source)['added'] == 0
    (source / '5_portfolio/decision.md').write_text(DECISION.replace('Underweight', 'Hold'), encoding='utf-8')
    (source / 'complete_report.md').write_text('changed', encoding='utf-8')
    (source / 'run_metadata.json').write_text(json.dumps({'ticker': '600941.SS', 'analysis_date': '2026-11-05'}), encoding='utf-8')
    assert store.import_directory(source)['added'] == 1
    reports = store.list_reports()
    assert len(reports) == 2
    newest = store.get_report(reports[0]['id'])
    assert newest['previous']['rating'] == 'Underweight'
    assert newest['changes'][0]['before'] == 'Underweight'
    assert store.get_report(reports[1]['id'])['rating'] == 'Underweight'
    (source / '5_portfolio/decision.md').unlink()
    assert store.get_report(reports[1]['id'])['rating'] == 'Underweight'


def test_bad_report_does_not_abort_import(tmp_path):
    source = tmp_path / 'source'
    sample_report(source / 'good')
    bad = source / 'bad'
    bad.mkdir()
    (bad / 'complete_report.md').write_bytes(b'\xff')
    store = DesktopStore(tmp_path / 'app')
    result = store.import_directory(source)
    assert result['added'] == 1
    assert len(result['errors']) == 1


def test_import_does_not_follow_directory_symlinks(tmp_path):
    source = tmp_path / 'source'
    sample_report(source / 'good')
    outside = sample_report(tmp_path / 'outside', date='2026-11-04')
    try:
        (source / 'linked').symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            pytest.skip('Windows account lacks symbolic-link privilege')
        raise
    store = DesktopStore(tmp_path / 'app')
    assert store.import_directory(source)['added'] == 1


def test_decisions_do_not_modify_holdings_and_unchanged_snapshot_is_noop(tmp_path):
    store = DesktopStore(tmp_path / 'app')
    store.import_directory(sample_report(tmp_path / 'report'))
    report = store.list_reports()[0]
    store.save_holding('600941.SS', 1000, 102, '券商核对')
    store.save_decision(report['id'], 'observe', '现金流需要继续核查')
    assert store.holdings()[0]['quantity'] == 1000
    assert len(store.history()) == 2
    store.save_holding('600941.SS', 1000, 102, '再次核对')
    assert len(store.history()) == 2
    with pytest.raises(ValueError):
        store.save_holding('600941.SS', -1, 102, '错误')
    with pytest.raises(ValueError):
        store.save_decision(report['id'], 'trade', '错误')


def test_search_paging_and_invalid_id(tmp_path):
    store = DesktopStore(tmp_path / 'app')
    store.import_directory(sample_report(tmp_path / 'report'))
    assert len(store.list_reports(query='600941', limit=1)) == 1
    assert store.list_reports(query='MSFT') == []
    assert store.list_reports(offset=1) == []
    with pytest.raises(KeyError):
        store.get_report('../../etc/passwd')


def test_review_header_is_preserved_when_legacy_decision_still_says_buy(tmp_path):
    source = sample_report(tmp_path / 'source', DECISION.replace('Underweight', 'Buy'))
    report_path = source / 'complete_report.md'
    report_path.write_text(report_path.read_text(encoding='utf-8') + '\n- Rating: REVIEW\n', encoding='utf-8')
    store = DesktopStore(tmp_path / 'app')
    store.import_directory(source)
    assert store.list_reports()[0]['rating'] == 'REVIEW'


def test_source_evidence_is_available_and_sanitized():
    doc = build_document('AAPL', '2026-11-05', {'decision': DECISION}, {
        'news_evidence': {'news:1': {'tool': 'get_news', 'status': 'available',
                                  'content': '<script>bad()</script>\n\nActual source text'}}})
    assert 'Actual source text' in doc['coverage'][0]['html']
    assert '<script>' not in doc['coverage'][0]['html']


def test_overview_and_combined_history_are_not_limited_to_first_hundred(tmp_path):
    from datetime import date, timedelta

    store = DesktopStore(tmp_path / 'app')
    for i in range(105):
        source = sample_report(tmp_path / f'report-{i}', date=(date(2026, 1, 1) + timedelta(days=i)).isoformat())
        if i == 0:
            file = source / 'complete_report.md'
            file.write_text(file.read_text(encoding='utf-8').replace('600941.SS', 'MSFT'), encoding='utf-8')
        store.import_directory(source)
    pending = store.pending_reports()
    assert {r['ticker'] for r in pending} == {'MSFT', '600941.SS'}
    assert store.history_entries(kind='research', offset=100)
    store.save_holding('MSFT', 10, 100, '实际持仓')
    assert store.holdings()[0]['report_date'] == '2026-01-01'
    oldest_id = next(r['id'] for r in pending if r['ticker'] == 'MSFT')
    store.save_decision(oldest_id, 'observe', '已核查')
    assert 'MSFT' not in {r['ticker'] for r in store.pending_reports()}
    assert len(store.history_entries(ticker='MSFT')) == 3


def test_current_grounded_writer_keeps_summary_facts_and_qualifications(tmp_path):
    from tradingagents.agents.grounding import GroundedReport, render_grounded_report
    from tradingagents.reporting import write_report_tree

    evidence = {'filing': {'tool': 'filing', 'status': 'available', 'content': 'Original source',
        'facts': [{'key': 'net', 'label': '净增', 'value': '-1454.60', 'unit': '万户',
                   'period': '2026Q3', 'basis': 'quarter flow', 'qualifiers': ['存量不等于净增']}]}}
    grounded = GroundedReport.model_validate({'summary': '变化值得关注，但仍需核查经营质量。', 'rating': 'Hold',
        'points': [{'kind': 'analysis', 'text': '经营承压，但不能由此推断偿付风险。', 'fact_ids': ['filing#net'],
                    'counter_fact_ids': ['filing#net']},
                   {'kind': 'scenario', 'text': '经营改善时重新评估。', 'fact_ids': ['filing#net']}]})
    state = {'company_of_interest': '600941.SS', 'report_contract_version': 1, 'final_rating': 'Hold', 'trade_date': '2026-11-05',
             'final_trade_decision': render_grounded_report(grounded, evidence),
             'fundamentals_evidence': evidence}
    source = tmp_path / 'source'
    write_report_tree(state, '600941.SS', source)
    store = DesktopStore(tmp_path / 'app')
    store.import_directory(source)
    doc = store.get_report(store.list_reports()[0]['id'])
    assert doc['summary']['conclusion']['text'] == grounded.summary
    assert '不能' in doc['summary']['reasons'][0]['text']
    assert 'filing#net' in doc['summary']['counter']['text']
    assert '重新评估' in doc['summary']['conditions']['text']
    facts = next(s for s in doc['sections'] if s['id'] == 'facts')
    serialized = json.dumps(facts, ensure_ascii=False)
    for value in ('-1454.60', '万户', '2026Q3', 'quarter flow', '存量不等于净增'):
        assert value in serialized
    assert '<table>' in serialized


def test_state_rating_and_evidence_corrections_create_new_snapshot(tmp_path):
    source = sample_report(tmp_path / 'source', DECISION.replace('Underweight', 'Buy'))
    logs = source / 'logs/AAPL/TradingAgentsStrategy_logs'
    logs.mkdir(parents=True)
    state_file = logs / 'full_states_log_2026-11-05.json'
    state_file.write_text(json.dumps({'final_rating': 'Buy'}), encoding='utf-8')
    store = DesktopStore(tmp_path / 'app')
    store.import_directory(source)
    state_file.write_text(json.dumps({'final_rating': 'REVIEW',
        'news_evidence': {'news': {'status': 'unavailable', 'content': 'Corrected source'}}}), encoding='utf-8')
    assert store.import_directory(source)['added'] == 1
    doc = store.get_report(store.list_reports()[0]['id'])
    assert doc['rating'] == 'REVIEW'
    assert 'Corrected source' in doc['coverage'][0]['html']


def test_export_keeps_review_warning_and_lossless_original():
    from tradingagents.desktop.reports import export_markdown

    doc = build_document('AAPL', '2026-11-05', {'decision': '**Rating**: Buy'}, {'final_rating': 'REVIEW'})
    doc['original_markdown'] = '# Original\n\n**Rating**: Buy\n\nOriginal facts and qualifications'
    text = export_markdown(doc)
    assert '有效状态：REVIEW' in text
    assert '需复核' in text
    assert doc['original_markdown'] in text
