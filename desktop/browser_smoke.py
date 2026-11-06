"""Optional layout/browser regression with the real local service bridge.

Requires Playwright for testing only; the desktop application does not use it.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from playwright.sync_api import sync_playwright

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.library import ReportLibrary
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.tasks import TaskManager

parser = argparse.ArgumentParser()
parser.add_argument('--report-dir', type=Path, required=True)
parser.add_argument('--output-dir', type=Path, default=Path('output/desktop'))
parser.add_argument('--chromium-path')
options = parser.parse_args()
options.output_dir.mkdir(parents=True, exist_ok=True)

with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright, ExitStack() as cleanup:
    store = DesktopStore(Path(directory))
    # Reproduce a holding entered in the old version with a bare mainland code.
    with store.connect() as db:
        db.execute('INSERT INTO holdings VALUES (?, ?, ?)', ('601658', 100, 3.1265))
        db.execute('INSERT INTO records VALUES (?, ?, NULL, ?, ?, ?, ?)',
                   ('legacy', '601658', 'holding', 'snapshot', '原始持仓核对', '2026-11-06'))
    incoming = Path(directory) / 'incoming'
    incoming.mkdir()
    library = ReportLibrary(store, [options.report_dir, incoming], interval=.25, settle_seconds=0)
    library.start()
    cleanup.callback(library.close)
    api = DesktopAPI(store, library=library)
    # Exercise the real scheduler and UI without making paid model calls.
    api._tasks = TaskManager(store, lambda _: [sys.executable, '-c', 'import time; time.sleep(30)'])
    cleanup.callback(api._tasks.close)
    methods = [name for name in dir(api) if not name.startswith('_') and callable(getattr(api, name))]
    browser = playwright.chromium.launch(executable_path=options.chromium_path)
    cleanup.callback(browser.close)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.expose_function('desktopCall', lambda method, args: getattr(api, method)(*args))
    page.add_init_script('window.pywebview={api:Object.fromEntries(' + repr(methods).replace("'", '"') +
                         '.map(name=>[name,(...args)=>window.desktopCall(name,args)]))};')
    page.goto(Path('tradingagents/desktop/static/index.html').resolve().as_uri())
    page.wait_for_selector('.page-head')
    assert page.title() == 'StockSeek'
    page.wait_for_selector('[data-inspect]')
    page.wait_for_selector('[data-angle="market"] .preview-point')
    assert [text.strip() for text in page.locator('.sidebar nav button').all_text_contents()] == [
        '今日关注', '股票研究', '持仓管理', '历史记录']
    assert page.locator('[data-angle="sentiment"] .angle-status').inner_text() == '数据不足'
    assert page.locator('.brand-mark img').evaluate('(image) => image.complete && image.naturalWidth > 0'), 'Brand image failed to load'
    execution=page.locator('[data-plan-field="execution"]')
    assert '101.2' in execution.inner_text() and '103.7' in execution.inner_text()
    assert '98.80' in execution.inner_text(), 'Defensive condition was lost'
    assert '未提供' in page.locator('[data-plan-field="horizon"]').inner_text()
    execution.locator('button').click()
    page.wait_for_selector('.source-block.highlight')
    assert '若跌破98.80' in page.locator('.source-block.highlight').inner_text()
    page.locator('#back').click()
    page.wait_for_selector('[data-angle="market"] .preview-point')
    page.screenshot(path=str(options.output_dir / 'today.png'), full_page=True)
    # Two source clicks on the same report must honor the latest click, even
    # when the earlier document request resolves after the newer one.
    expected_source = page.locator('[data-angle="fundamentals"] .preview-point').first.get_attribute('data-preview-source')
    page.evaluate('''() => {
        const original=window.pywebview.api.get_report;
        window.restoreReportAPI=()=>window.pywebview.api.get_report=original;
        let first=true;
        window.pywebview.api.get_report=async id=>{
            const delay=first;first=false;const response=await original(id);
            if(delay)await new Promise(resolve=>setTimeout(resolve,400));
            return response;
        };
        document.querySelector('[data-angle="market"] .preview-point').click();
        document.querySelector('[data-angle="fundamentals"] .preview-point').click();
    }''')
    page.wait_for_selector('.source-block.highlight')
    page.wait_for_timeout(550)
    assert page.locator('.source-block.highlight').get_attribute('id') == expected_source, 'An obsolete source click overrode the latest choice'
    page.evaluate('window.restoreReportAPI()')
    page.locator('#back').click()
    page.wait_for_selector('[data-angle="market"] .preview-point')
    page.set_viewport_size({'width': 1024, 'height': 700})
    page.wait_for_timeout(100)
    footer_before = page.locator('.preview-actions').bounding_box()
    assert footer_before['y'] + footer_before['height'] <= 700
    page.locator('#preview-scroll').evaluate('(el) => el.scrollTop=el.scrollHeight')
    footer_after = page.locator('.preview-actions').bounding_box()
    assert abs(footer_before['y'] - footer_after['y']) < 1
    # Clicking a preview point opens its own original source and can return home.
    page.locator('[data-angle="market"] .preview-point').first.click()
    page.wait_for_selector('.source-block.highlight')
    assert '动能已转弱' in page.locator('.source-block.highlight').inner_text()
    page.locator('#back').click()
    page.wait_for_selector('[data-angle="market"] .preview-point')
    assert page.locator('.preview-stock').inner_text().startswith('中国移动')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('.sidebar [data-page="research"]').click()
    page.locator('#content [data-report]').first.click()
    page.wait_for_selector('.conclusion')
    assert page.locator('.argument').count() == 3
    page.screenshot(path=str(options.output_dir / 'summary.png'), full_page=True)
    page.locator('[data-source]').first.click()
    page.wait_for_selector('.source-block.highlight')
    page.locator('[data-tab="full"]').click()
    page.locator('#reader-search').fill('现金流')
    page.locator('[data-action="reader-find"]').click()
    assert page.locator('.source-block.highlight').count() == 1
    page.locator('[data-tab="summary"]').click()
    for width in (760, 1024, 1440):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.locator('.sidebar [data-page="research"]').click()
    # A new report arriving after launch must become visible without an import click.
    fresh = incoming / 'new-report'
    fresh.mkdir()
    (fresh / 'complete_report.md').write_text(
        '# Trading Analysis Report: MSFT\n- Analysis date: 2026-11-06\n\n'
        '## V. Portfolio Manager Decision\n\n**Rating**: Hold\n\n'
        '**Executive Summary**: 测试研究：维持 Hold，非 Sell。 [引用：market:snapshot:1#close]\n\n'
        '**配置期限**：一个季度，披露时复核。\n\n**执行方式**：等待，不执行。\n\n'
        '**执行条件**：等待有来源依据的确认信号，暂无即时交易建议。\n\n'
        '**计划有效期**：下次披露或事件窗口变化时重审。\n\n'
        '**失效条件**：关键证据发生变化时重新评估。\n\n'
        '**事件窗口**：覆盖不足，不能断言没有催化。', encoding='utf-8')
    page.wait_for_selector('.research-table:has-text("MSFT")')
    page.locator('.sidebar [data-page="today"]').click()
    page.locator('.queue-row:has-text("MSFT")').click()
    page.wait_for_function("[...document.querySelectorAll('.angle-status')].length===4 && [...document.querySelectorAll('.angle-status')].every(el=>el.textContent==='未提供')")
    assert 'MSFT' in page.locator('.preview-stock').inner_text()
    assert '维持 持有，非 卖出' in page.locator('.preview-verdict').inner_text()
    assert 'market:snapshot:1#close' not in page.locator('.preview-verdict').inner_text()
    page.locator('.preview-verdict button').click()
    page.wait_for_selector('.source-block.highlight')
    technical=page.locator('.source-block.highlight .technical-references')
    assert technical.count() == 1
    assert technical.locator('pre').is_hidden()
    technical.locator('summary').click()
    assert 'market:snapshot:1#close' in technical.inner_text()
    page.locator('#back').click()
    page.wait_for_selector('.preview-verdict')
    assert '中国移动' not in page.locator('#preview-scroll').inner_text()
    assert '一个季度' in page.locator('[data-plan-field="horizon"]').inner_text()
    assert '等待，不执行' in page.locator('[data-plan-field="mode"]').inner_text()
    page.screenshot(path=str(options.output_dir / 'structured-plan.png'), full_page=True)
    page.locator('[data-plan-field="invalidation"] button').click()
    page.wait_for_selector('.source-block.highlight')
    assert '关键证据发生变化' in page.locator('.source-block.highlight').inner_text()
    page.locator('[data-tab="summary"]').click()
    assert '等待，不执行' in page.locator('.report-plan').inner_text()
    page.locator('.report-plan [data-plan-field="execution"] button').click()
    page.wait_for_selector('.source-block.highlight')
    assert '暂无即时交易建议' in page.locator('.source-block.highlight').inner_text()

    page.locator('.sidebar [data-page="research"]').click()
    page.locator('#report-search').fill('不存在')
    page.wait_for_selector('#content .empty')
    page.locator('.sidebar [data-page="today"]').click()
    assert page.locator('[data-inspect]').count() > 0, 'Search must not filter home research'
    page.locator('.sidebar [data-page="history"]').click()
    page.wait_for_selector('.record')
    assert page.locator('[data-report]').count() > 0, 'Search must not filter history'
    page.locator('#history-ticker').fill('邮储')
    history_menu = page.get_by_role('button', name='记录类型', exact=True)
    history_menu.click()
    page.get_by_role('listbox', name='记录类型').screenshot(path=str(options.output_dir / 'history-dropdown.png'))
    page.get_by_role('option', name='实际持仓', exact=True).click()
    assert page.locator('#history-kind').input_value() == 'holding'
    page.locator('[data-action="filter-history"]').click()
    page.wait_for_selector('.record-title:has-text("邮储银行")')
    history_menu = page.get_by_role('button', name='记录类型', exact=True)
    history_menu.focus()
    page.keyboard.press('ArrowDown')
    page.keyboard.press('Home')
    page.keyboard.press('Enter')
    assert page.locator('#history-kind').input_value() == ''
    page.locator('#history-ticker').fill('')
    page.locator('[data-action="filter-history"]').click()
    page.locator('.sidebar [data-page="holdings"]').click()
    page.wait_for_selector('td:has-text("邮储银行")')
    assert page.locator('td:has-text("3.1265")').count() == 1
    page.locator('[data-holding="601658.SS"]').click()
    page.wait_for_selector('#holding-stock-match:has-text("邮储银行")')
    page.locator('#holding-dialog').screenshot(path=str(options.output_dir / 'holding-dialog.png'))
    page.locator('#holding-form [name="cost"]').fill('3.1265')
    page.locator('#holding-form [type="submit"]').click()
    page.wait_for_function("!document.querySelector('#holding-dialog').open")
    assert len(store.holdings()) == 1, 'Legacy alias must not duplicate holdings'
    page.locator('[data-action="new-holding"]').first.click()
    form = page.locator('#holding-form')
    form.locator('[type="submit"]').click()
    page.wait_for_selector('#holding-form .field-error')
    form.locator('[name="ticker"]').fill('600941.SS')
    form.locator('[name="quantity"]').fill('1000')
    form.locator('[name="cost"]').fill('102')
    form.locator('[name="reason"]').fill('Browser smoke snapshot')
    form.locator('[type="submit"]').click()
    page.wait_for_selector('[data-holding="600941.SS"]')
    page.reload()
    page.wait_for_selector('.page-head')
    page.locator('.sidebar [data-page="holdings"]').click()
    assert page.locator('[data-holding="600941.SS"]').count() == 1, 'Persisted snapshot missing'
    page.locator('.sidebar [data-page="today"]').click()
    page.wait_for_selector('.preview-actions')
    page.evaluate("window.pywebview.api.start_analysis({ticker:'AAPL',date:'2026-11-05',provider:'openai',quick_model:'test',deep_model:'test'})")
    page.wait_for_selector('#task-strip.running')
    page.wait_for_timeout(100)
    preview_footer = page.locator('.preview-actions').bounding_box()
    assert preview_footer['y'] + preview_footer['height'] <= page.viewport_size['height'], 'Task strip pushed preview actions below the window'
    page.locator('#task-strip [data-action="cancel"]').click()
    page.wait_for_function("!document.querySelector('#task-strip').classList.contains('running')")
    page.locator('.topbar [data-page="analysis"]').click()
    page.locator('#analysis-form [name="ticker"]').fill('AAPL')
    page.locator('#analysis-form [type="submit"]').click()
    page.wait_for_selector('#task-strip.running')
    assert page.locator('#analysis-form [type="submit"]').is_disabled()
    page.locator('.sidebar [data-page="history"]').click()
    page.wait_for_selector('.record [data-report]')
    page.locator('.record [data-report]').first.click()
    page.wait_for_selector('.conclusion')
    page.evaluate('window.scrollTo(0,700)')
    page.wait_for_timeout(100)
    bounds = page.evaluate('''() => ({tabs:document.querySelector('.tabs').getBoundingClientRect().top,
                                  strip:document.querySelector('#task-strip').getBoundingClientRect().bottom})''')
    assert bounds['tabs'] >= bounds['strip'] - 1, bounds
    page.locator('#task-strip [data-action="cancel"]').click()
    page.wait_for_function("!document.querySelector('#task-strip').classList.contains('running')")
    api._tasks.close()
    assert not errors, errors
    browser.close()
    print('Browser regression passed: Chinese stock names, legacy holdings, themed dropdown/keyboard, inline validation, auto reports, responsive layout, persistence and task/cancel; no JS errors')
