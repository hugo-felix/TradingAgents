"""Optional layout/browser regression with the real local service bridge.

Requires Playwright for testing only; the desktop application does not use it.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.library import ReportLibrary
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.tasks import TaskManager

parser = argparse.ArgumentParser()
parser.add_argument('--report-dir', type=Path,
                    default=Path(__file__).parent / 'fixtures' / 'research')
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
    progress_script = (
        "import json,time; print('TA_DESKTOP_EVENT:' + json.dumps("
        "{'type':'phase','phase':'正在分析行情、基本面与情绪',"
        "'completed':['initialize','news_report'],"
        "'active':['market_report','fundamentals_report','sentiment_report']}),flush=True);"
        "time.sleep(30)"
    )
    api._tasks = TaskManager(store, lambda _: [sys.executable, '-c', progress_script])
    cleanup.callback(api._tasks.close)
    cleanup.callback(api._book_jobs.close)
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
        '今日关注', '股票研究', '高股息', '持仓管理', '历史记录', '成交复盘', '书籍资料', '策略管理']
    assert page.locator('[data-angle="sentiment"] .angle-status').inner_text() == '数据不足'
    assert page.locator('.brand-mark img').evaluate('(image) => image.complete && image.naturalWidth > 0'), 'Brand image failed to load'
    execution=page.locator('[data-plan-field="execution"]')
    assert '101.2' in execution.inner_text() and '103.7' in execution.inner_text()
    assert '98.80' in execution.inner_text(), 'Defensive condition was lost'
    assert page.locator('[data-plan-field="horizon"]').is_hidden()
    assert page.locator('[data-plan-field="execution"]').is_visible()
    assert page.locator('[data-plan-field="invalidation"]').is_visible()
    page.locator('.plan-more > summary').click()
    assert '未提供' in page.locator('[data-plan-field="horizon"]').inner_text()
    page.locator('.plan-more > summary').click()
    supporting = page.locator('.angle-more').first
    assert supporting.count() == 1, 'Fixture must exercise supporting context'
    assert supporting.locator('.supporting-point').first.is_hidden()
    supporting.locator('summary').click()
    assert supporting.locator('.supporting-point').first.is_visible()
    supporting.locator('summary').click()
    execution.locator('button').click()
    page.wait_for_selector('.source-block.highlight')
    assert '若跌破98.80' in page.locator('.source-block.highlight').inner_text()
    page.locator('#back').click()
    page.wait_for_selector('[data-angle="market"] .preview-point')
    # Check allocation as well as overflow, including native minimum and fullscreen.
    for width in (760, 900, 901, 1024, 1440, 1920, 2560):
        page.set_viewport_size({'width': width, 'height': 900})
        page.wait_for_timeout(100)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        main = page.locator('main').bounding_box()
        workspace = page.locator('.workspace').bounding_box()
        assert abs(main['x'] - workspace['x']) < 1, width
        assert abs(main['width'] - workspace['width']) < 1, width
        queue = page.locator('.queue').bounding_box()
        preview = page.locator('#today-detail').bounding_box()
        if width <= 900:
            assert preview['y'] >= queue['y'] + queue['height'], width
            assert page.locator('#today-detail').evaluate('(el) => el.style.height') == ''
            assert page.locator('#preview-scroll').evaluate(
                '(el) => getComputedStyle(el).overflowY') == 'visible'
        else:
            assert 279 <= queue['width'] <= 381, (width, queue)
            assert preview['width'] > queue['width'], width
            footer = page.locator('.preview-actions').bounding_box()
            assert footer['y'] + footer['height'] <= 900, width
        if width in (760, 2560):
            page.screenshot(path=str(options.output_dir / f'today-{width}.png'), full_page=True)
    page.set_viewport_size({'width': 1440, 'height': 1000})
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
    for width in (760, 900, 1024, 1440, 1920, 2560):
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
    queue_excerpt = page.locator('.queue-row:has-text("MSFT") .preview-text').inner_text()
    assert '维持 持有，非 卖出' in queue_excerpt
    assert 'market:snapshot:1#close' not in queue_excerpt
    assert '模型观点' in page.locator('.preview-rating').inner_text()
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
    assert page.locator('[data-plan-field="horizon"]').is_hidden()
    page.locator('.plan-more > summary').click()
    assert '一个季度' in page.locator('[data-plan-field="horizon"]').inner_text()
    page.locator('.plan-more > summary').click()
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
    assert '研究当前情况请选择今天' in page.locator('#analysis-date-help').inner_text()
    assert page.locator('#analysis-form [name="date"]').get_attribute('aria-describedby') == 'analysis-date-help'
    page.locator('#analysis-form [name="ticker"]').fill('AAPL')
    page.locator('#analysis-form [type="submit"]').click()
    page.wait_for_selector('#task-strip.running')
    assert page.locator('#analysis-form [type="submit"]').is_disabled()
    page.wait_for_selector('.progress-caption:has-text("已完成 2 / 9 项")')
    assert page.locator('.progress-steps .completed').count() == 2
    assert page.locator('.progress-steps .running').count() == 3
    assert '当前阶段耗时' in page.locator('.progress-caption').inner_text()
    for width in (760, 900, 1024, 1440, 1920, 2560):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
    page.locator('#task-detail').screenshot(path=str(options.output_dir / 'analysis-progress.png'))
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
    page.locator('.topbar [data-page="analysis"]').click()
    page.wait_for_selector('#task-detail:has-text("分析已取消")')
    assert page.locator('.progress-steps .running').count() == 0
    assert page.locator('.progress-caption').inner_text().startswith('已完成 2 / 9 项')
    api._tasks.close()
    # Replay interrupted snapshots only in this isolated smoke workspace.
    with api._tasks.lock:
        api._tasks.current.update(status='interrupted', phase='上次运行已中断，可重新分析',
                                  started_epoch=1793923200,
                                  last_activity_at='2026-11-06T00:03:00+00:00')
    page.wait_for_selector('#task-detail:has-text("截至最后活动约耗时 3 分 0 秒")')
    with api._tasks.lock:
        api._tasks.current.pop('last_activity_at')
    page.wait_for_selector('#task-detail:has-text("耗时未知")')
    # Offline dividend snapshot: exercise dashboard sorting, threshold views,
    # filtering and evidence without calling market providers.
    page.evaluate('''() => {
        const items=[
            ['600941','中国移动',5.2,'pass'],
            ['601398','工商银行',4.3,'pending'],
            ['601318','中国平安',3.8,'fail']
        ].map(([code,name,yield_pct,status])=>({code,name,sector:'离线验收',status,
            metrics:{yield_pct,dividend_year:2025,yield_ttm_pct:yield_pct,
                market_cap_yi:2000,pe_annual:12,volatility_1y_pct:18,
                max_drawdown_1y_pct:15,turnover_20d_yi:6},
            checks:[],core:{status,checks:[]}}));
        window.pywebview.api.dividend_screen=async()=>({ok:true,data:{
            schema_version:2,generated_at:new Date().toISOString(),as_of:'2026-11-08',
            coverage:'离线界面验收数据',rules:'年度已实施股息率 >4%',
            core_rules:'分红、财务与低波门槛',limitations:'仅供界面验收',
            discovery:{risk_complete:3,provider_error_count:0},items
        }});
    }''')
    page.locator('.sidebar [data-page="dividends"]').click()
    page.wait_for_selector('.dividend-table tbody tr')
    assert '市场覆盖尚未对账' in page.locator('#content').inner_text()
    assert '未确认' in page.locator('.dividend-health').inner_text()
    assert '年度股息率缺失' in page.locator('.dividend-health').inner_text()
    assert '缺失不视为零' in page.locator('.dividend-health').inner_text()
    assert 'TTM 仅供参考' in page.locator('.dividend-yield-definition').inner_text()
    page.locator('summary:has-text("更新与连接")').click()
    automatic = page.locator('[data-action="dividend-auto"]')
    assert automatic.get_attribute('aria-pressed') == 'true'
    automatic.click()
    page.wait_for_selector('[data-action="dividend-auto"][aria-pressed="false"]', state='attached')
    assert api._dividends.status()['auto_update'] is False
    assert page.locator('.dividend-table tbody tr').count() == 1
    page.locator('[data-filter="yield"]').click()
    assert page.locator('.dividend-table tbody tr').count() == 2
    assert '中国移动' in page.locator('.dividend-table tbody tr').first.inner_text()
    page.locator('[data-sort="yield_pct"]').click()
    assert '工商银行' in page.locator('.dividend-table tbody tr').first.inner_text()
    assert page.locator('th[aria-sort="ascending"]').count() == 1
    page.locator('[data-action="dividend-evidence"]').first.click()
    assert '工商银行' in page.locator('dialog[open]').inner_text()
    page.keyboard.press('Escape')
    assert page.locator('dialog[open]').count() == 0
    page.locator('#dividend-filters [name="query"]').fill('600941')
    page.locator('#dividend-filters [type="submit"]').click()
    assert page.locator('.dividend-table tbody tr').count() == 1
    page.locator('[data-action="dividend-reset"]').click()
    assert page.locator('.dividend-table tbody tr').count() == 2
    for width in (760, 1024, 1440, 1920, 2560):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        table = page.locator('.dividend-table-scroll')
        table.evaluate('(el)=>el.scrollLeft=el.scrollWidth')
        assert page.locator('.dividend-table td').first.bounding_box()['x'] >= table.bounding_box()['x'] - 1
        table.evaluate('(el)=>el.scrollLeft=0')
        page.screenshot(path=str(options.output_dir / f'dividends-{width}.png'), full_page=True)
    # Empty-position planning uses only this isolated workspace and offline data.
    with store.connect() as db:
        db.execute('DELETE FROM holdings')
    api._read_dividend_screen = lambda: {
        'as_of': '2026-11-08', 'generated_at': '2026-11-08T18:00:00+08:00',
        'items': [{'code': '600941', 'name': '中国移动', 'sector': '电信',
                   'status': 'pass', 'core': {'status': 'pass'},
                   'metrics': {'yield_pct': 5.2, 'dividend_annual': 4.999,
                               'dividend_year': 2025, 'price': 96.13}}]}
    page.reload()
    page.wait_for_selector('[data-entry-code="600941"]')
    assert '先设置目标' in page.locator('.entry-table').inner_text()
    assert page.locator('.entry-progress strong').all_text_contents() == ['1只', '0只', '1只', '0只']
    form = page.locator('#entry-preferences-form')
    for key, value in {'capital': '100000', 'target_yield': '5',
                       'position_pct': '20', 'horizon_years': '3'}.items():
        form.locator(f'[name="{key}"]').fill(value)
    form.locator('[type="submit"]').click()
    page.wait_for_selector('.entry-table:has-text("99.98元")')
    assert '20,000' in page.locator('.entry-settings summary').inner_text()
    assert not page.locator('.entry-settings').evaluate('(el)=>el.open')
    assert '待核验' in page.locator('.entry-table').inner_text()
    for width in (760, 1024, 1440, 2560):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.screenshot(path=str(options.output_dir / f'entry-{width}.png'), full_page=True)
    page.reload()
    page.wait_for_selector('.entry-table:has-text("99.98元")')
    page.locator('.entry-settings summary').click()
    assert float(form.locator('[name="target_yield"]').input_value()) == 5
    # Completed research has an explicit handoff to a personal plan, not a trade.
    row = page.locator('[data-entry-code="600941"]')
    row.locator('button:not([data-judgment])').click()
    page.wait_for_selector('.research-next')
    page.locator('.research-next [data-judgment]').click()
    page.wait_for_selector('#decision-form:not([hidden])')
    assert page.locator('#decision-form [name="action"]').input_value() == 'observe'
    page.locator('#decision-form [name="reason"]').fill('等待价格条件，周一核对实时行情；未成交。')
    page.locator('#decision-form [type="submit"]').click()
    page.wait_for_selector('.timeline .record:has-text("等待价格条件")')
    assert not store.holdings(), 'A personal plan must not become a holding'
    page.get_by_role('button', name='返回今日关注', exact=True).click()
    page.wait_for_selector('[data-entry-code="600941"] .entry-decision')
    assert '继续观察' in row.inner_text()
    assert '等待价格条件' in row.inner_text()
    page.reload()
    page.wait_for_selector('[data-entry-code="600941"] .entry-decision')
    assert '等待价格条件' in row.inner_text(), 'Personal plan must survive reload'
    assert page.locator('.entry-progress strong').all_text_contents() == ['1只', '0只', '0只', '1只']
    # Yield ordering must not hide a sixth candidate or cap one industry.
    sample = api._read_dividend_screen()['items'][0]
    api._read_dividend_screen = lambda: {
        'as_of': '2026-11-08', 'generated_at': '2026-11-08T18:00:00+08:00',
        'items': [{**sample, 'code': f'60000{i}', 'sector': '银行'} for i in range(6)]}
    page.reload()
    page.wait_for_selector('[data-entry-code="600005"]')
    assert page.locator('[data-entry-code]').count() == 6
    assert '非买入优先级' in page.locator('.entry-preparation').inner_text()
    assert '先补环境数据' in page.locator('.entry-preparation').inner_text()
    assert page.locator('.market-metrics').count() == 0
    # Explain each market warning without hiding candidates or inventing missing metrics.
    environment_sample = api._read_dividend_screen()
    api._read_dividend_screen = lambda: {
        **environment_sample,
        'market_context': {'as_of': '2026-11-08', 'method': '研究预设，未经回测',
                           'market': {'status': 'caution', 'as_of': '2026-11-06',
                                      'reason': '60日最大回撤16.67% > 15%',
                                      'close': 100, 'sma_120': 110,
                                      'sma_slope_20_pct': -2,
                                      'volatility_20d_pct': 17.5,
                                      'drawdown_60d_pct': 16.67,
                                      'endpoint': 'index_zh_a_hist', 'symbol': '000300'}}}
    page.reload()
    page.wait_for_selector('.market-detail')
    assert page.locator('#entry-environment-job[role="status"]').count() == 1
    assert not page.locator('.market-detail').evaluate('(el)=>el.open')
    for width in (1440, 2560):
        page.set_viewport_size({'width': width, 'height': 1000})
        page.screenshot(path=str(options.output_dir / f'today-compact-{width}.png'), full_page=True)
    page.locator('.market-detail summary').click()
    assert '16.67%' in page.locator('.market-metrics').inner_text()
    assert '17.50%' in page.locator('.market-metrics').inner_text()
    page.locator('.market-basis summary').click()
    assert 'index_zh_a_hist' in page.locator('.market-basis').inner_text()
    assert '时间待核验' in page.locator('.market-basis').inner_text()
    for width in (760, 1440, 2560):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.screenshot(path=str(options.output_dir / f'market-{width}.png'), full_page=True)
    # A candidate without local research only prefills configuration.
    api._read_dividend_screen = lambda: {
        'as_of': '2026-11-08', 'generated_at': '2026-11-08T18:00:00+08:00',
        'items': [{'code': '600036', 'name': '招商银行', 'sector': '银行',
                   'status': 'pass', 'core': {'status': 'pass'},
                   'metrics': {'yield_pct': 5.2, 'dividend_annual': 2.1}}]}
    page.reload()
    page.wait_for_selector('[data-action="entry-research"]')
    task_id = api._tasks.status()['id']
    page.locator('[data-action="entry-research"]').click()
    assert page.locator('#analysis-form [name="ticker"]').input_value() == '600036'
    assert api._tasks.status()['id'] == task_id
    assert not store.holdings()
    # Book management uses real storage/API and a synthetic native picker result.
    # No real book, OCR, model call or native window is needed for this browser check.
    with patch.dict(sys.modules, {'webview': SimpleNamespace(
            FileDialog=SimpleNamespace(OPEN='open'))}):
        source = Path(directory) / 'synthetic.pdf'
        source.write_bytes(b'%PDF-1.7\nsynthetic browser fixture\n%%EOF')
        api._window = SimpleNamespace(create_file_dialog=lambda *a, **k: None)
        page.locator('.sidebar [data-page="books"]').click()
        page.wait_for_selector('.empty:has-text("尚未登记书籍")')
        book_form = page.locator('#book-form')
        book_form.locator('[type="submit"]').click()
        assert book_form.locator('[aria-invalid="true"]').count() == 7
        metadata = {'title': '<script>测试书籍</script>', 'author': '合成作者',
                    'translator': '无', 'language': '中文', 'edition': '测试版',
                    'source': '离线合成文件', 'usage_scope': '浏览器回归'}
        for key, value in metadata.items():
            book_form.locator(f'[name="{key}"]').fill(value)
        book_form.locator('[type="submit"]').click()
        page.wait_for_selector('#toast:has-text("已取消选择")')
        assert book_form.locator('[name="title"]').input_value() == metadata['title']
        assert api.list_books()['data'] == []
        api._window = SimpleNamespace(create_file_dialog=lambda *a, **k: (str(source),))
        book_form.locator('[type="submit"]').click()
        page.wait_for_selector('.book-record')
        assert page.locator('.book-record h3').inner_text() == metadata['title']
        assert page.locator('.book-record script').count() == 0
        assert '尚无已发布文本 · 语义索引未建立' in page.locator('.book-record').inner_text()
        book_form.locator('[type="submit"]').click()
        page.wait_for_selector('#toast:has-text("此文件已登记")')
        assert page.locator('.book-record').count() == 1
        book_form.locator('[name="edition"]').fill('冲突版')
        book_form.locator('[type="submit"]').click()
        page.wait_for_selector('#toast.error')
        assert len(api.list_books()['data']) == 1
        assert book_form.locator('[name="edition"]').input_value() == '冲突版'
        page.locator('[data-action="book-verify"]').click()
        page.wait_for_selector('.book-check:has-text("指纹一致")')
        book = api.list_books()['data'][0]
        snapshot = store.directory / 'books' / 'originals' / book['sha256']
        snapshot.write_bytes(b'changed fixture')
        page.locator('[data-action="book-verify"]').click()
        page.wait_for_selector('.book-check:has-text("原文快照已变更")')
        snapshot.unlink()
        page.locator('[data-action="book-verify"]').click()
        page.wait_for_selector('.book-check:has-text("原文快照缺失")')
        page.locator('.book-record summary').click()
        assert book['sha256'] in page.locator('.book-record').inner_text()
        for width in (760, 1024, 1440, 2560):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
            if width == 1440:
                page.screenshot(path=str(options.output_dir / 'books.png'), full_page=True)
        # A failed refresh must remain distinct from an empty registry.
        page.evaluate('''() => {
            window.restoreBookList=window.pywebview.api.list_books;
            window.pywebview.api.list_books=async()=>({ok:false,error:'合成读取错误'});
        }''')
        page.locator('[data-action="books-reload"]').click()
        page.wait_for_selector('[role="alert"]:has-text("合成读取错误")')
        assert page.locator('.book-record').count() == 1
        page.evaluate('window.pywebview.api.list_books=window.restoreBookList')
        page.locator('[data-action="books-reload"]').click()
        page.wait_for_selector('[data-action="books-reload"]:not([disabled])')
        assert page.locator('[role="alert"]').count() == 0
        page.reload()
        page.wait_for_selector('.page-head')
        page.locator('.sidebar [data-page="books"]').click()
        page.wait_for_selector('.book-record')
        assert page.locator('.book-check').inner_text() == '尚未核验原文快照'
        assert page.locator('.book-record h3').inner_text() == metadata['title']
    # Text extraction uses a synthetic EPUB, with no network, OCR or model calls.
    import zipfile
    with tempfile.TemporaryDirectory() as text_directory:
        source = Path(text_directory) / 'text.epub'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml',
                             '<container><rootfile full-path="book.opf"/></container>')
            archive.writestr('book.opf', '<package><manifest><item id="c" href="c.xhtml" '
                             'media-type="application/xhtml+xml"/></manifest>'
                             '<spine><itemref idref="c"/></spine></package>')
            archive.writestr('c.xhtml', '<html><body>volume 趋势确认 &lt;img src=x&gt;'
                             '<script>window.bookInjected=true</script></body></html>')
        api._window = SimpleNamespace(create_file_dialog=lambda *a, **kw: (str(source),))
        text_book = api.register_book({
            'title': '原文检索样本', 'author': '合成作者', 'translator': '无',
            'language': '中文 / English', 'edition': '测试版',
            'source': '合成 EPUB', 'usage_scope': '仅回归测试',
        })['data']['book']
        page.locator('[data-action="books-reload"]').click()
        record = page.locator(f'[data-book-id="{text_book["book_id"]}"]')
        record.locator('[data-action="book-parse"]').click()
        record.locator('[data-book-parse]').wait_for(state='attached')
        record.locator('[data-book-query]').fill('volume')
        record.locator('[data-action="book-search"]').click()
        record.locator('details:has([data-action="book-citation"]) > summary').click()
        record.locator('[data-action="book-citation"]').click()
        record.locator('.book-citation').wait_for()
        assert '趋势确认 <img src=x>' in record.locator('.book-citation').inner_text()
        assert record.locator('.book-citation img, .book-citation script').count() == 0
        assert not page.evaluate('Boolean(window.bookInjected)')
        for width in (760, 1024, 1440, 2560):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.reload()
        page.wait_for_selector('.page-head')
        page.locator('.sidebar [data-page="books"]').click()
        record.locator('[data-book-parse]').wait_for(state='attached')
        record.locator('[data-book-query]').fill('absent')
        record.locator('[data-action="book-search"]').click()
        record.locator('[role="status"]:has-text("共 0 个位置")').wait_for()
        (store.directory / 'books' / 'originals' / text_book['sha256']).unlink()
        record.locator('[data-action="book-parse"]').click()
        record.locator('[role="alert"]:has-text("本地解析失败")').wait_for()
        assert record.locator('[data-book-parse] option').count() == 1
        api._window = None
    # Strategy definitions are synthetic and never execute market/model requests.
    page.locator('.sidebar [data-page="strategies"]').click()
    page.wait_for_selector('.empty:has-text("尚未保存策略")')
    page.locator('[data-action="strategy-new"]').click()
    strategy_form = page.locator('#strategy-form')
    strategy_form.locator('[type="submit"]').click()
    assert strategy_form.locator('[aria-invalid="true"]').count() == 1
    strategy_form.locator('[name="name"]').fill('<script>离线策略</script>')
    strategy_form.locator('[type="submit"]').click()
    review = page.locator('[aria-label="策略版本详情"]')
    page.wait_for_selector('[aria-label="策略版本详情"] h2:has-text("v1")')
    assert review.locator('script').count() == 0
    confirm = page.locator('#strategy-confirm-form')
    confirm.locator('[type="submit"]').click()
    assert confirm.locator('[aria-invalid="true"]').count() == 2
    confirm.locator('[name="confirmed_by"]').fill('离线核对人')
    confirm.locator('[type="checkbox"]').check()
    confirm.locator('[type="submit"]').click()
    page.wait_for_selector('#toast.error:has-text("尚未完整")')
    assert api.list_strategies()['data'][0]['version'] == 1
    page.locator('[data-action="strategy-edit"]').click()
    scope = {'source': '个人测试说明 v1', 'market': '合成市场', 'universe': '合成股票池',
             'timeframe': '日线', 'holding_period': '仅作测试', 'environment': '离线环境',
             'observation_time': '收盘后', 'confirmation_time': '核对后',
             'execution_time': '不执行', 'execution_price': '不产生成交'}
    for key, value in scope.items():
        strategy_form.locator(f'[name="{key}"]').fill(value)
    groups = ('entry', 'filters', 'exit', 'invalidation', 'position', 'portfolio', 'exceptions')
    for group in groups:
        page.locator(f'[data-action="strategy-add-rule"][data-group="{group}"]').click()
        row = strategy_form.locator(f'[data-rule-row][data-group="{group}"]').first
        row.locator('[data-rule-field="text"]').fill(f'{group} 合成规则 <b>原样文本</b>')
        row.locator('[data-rule-field="citation"]').fill('个人测试说明，不是书籍原文')
    # Add/remove must retain other rows and scope; navigation retains the window draft.
    page.locator('[data-action="strategy-add-rule"][data-group="entry"]').click()
    page.locator('[data-action="strategy-remove-rule"][data-group="entry"]').last.click()
    page.locator('.sidebar [data-page="books"]').click()
    page.locator('.sidebar [data-page="strategies"]').click()
    assert strategy_form.locator('[name="market"]').input_value() == scope['market']
    assert strategy_form.locator('[data-rule-row]').count() == 7
    assert 'entry 合成规则' in strategy_form.locator('[data-rule-field="text"]').first.input_value()
    for width in (760, 1024, 1440, 2560):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
    page.set_viewport_size({'width': 1440, 'height': 1000})
    strategy_form.locator('[type="submit"]').click()
    page.wait_for_selector('[aria-label="策略版本详情"] h2:has-text("v2")')
    confirm.locator('[name="confirmed_by"]').fill('离线核对人')
    confirm.locator('[type="checkbox"]').check()
    confirm.locator('[type="submit"]').click()
    page.wait_for_selector('#toast.error:has-text("待定义规则")')
    page.locator('[data-action="strategy-edit"]').click()
    for group in groups:
        strategy_form.locator(f'[data-group="{group}"] [data-rule-field="state"]').select_option('manual', force=True)
    strategy_form.locator('[type="submit"]').click()
    page.wait_for_selector('[aria-label="策略版本详情"] h2:has-text("v3")')
    confirm.locator('[name="confirmed_by"]').fill('离线核对人')
    confirm.locator('[type="checkbox"]').check()
    confirm.locator('[type="submit"]').click()
    page.wait_for_selector('[aria-label="策略版本详情"] h2:has-text("v4 · 已确认")')
    assert '离线核对人' in review.inner_text()
    assert '实现未完成 · 回测未评估 · 样本外未评估' in review.inner_text()
    page.locator('[data-action="strategy-version"][data-version="1"]').click()
    assert review.locator('[data-action="strategy-edit"]').count() == 0
    assert '未填写' in review.inner_text()
    page.locator('[data-action="strategy-version"][data-version="4"]').click()
    page.locator('[data-action="strategy-edit"]').click()
    strategy_form.locator('[name="name"]').fill('新一版个人草稿')
    assert page.locator('#strategy-confirm-form').count() == 0
    strategy_form.locator('[type="submit"]').click()
    page.wait_for_selector('[aria-label="策略版本详情"] h2:has-text("v5 · 草稿")')
    doc = api.list_strategies()['data'][0]
    assert doc['confirmation'] is None
    # Preserve fields that future/API clients may add when editing through the UI.
    doc['definition']['extra_context'] = {'source_version': 'synthetic'}
    doc['definition']['rules']['entry'][0]['extra_evidence'] = ['synthetic']
    assert api.save_strategy(doc['definition'], doc['strategy_id'], 5)['ok']
    page.locator('[data-action="strategy-open"]').click()
    page.wait_for_selector('[aria-label="策略版本详情"] h2:has-text("v6")')
    page.locator('[data-action="strategy-edit"]').click()
    strategy_form.locator('[name="environment"]').fill('保留的未保存编辑')
    # Another writer advances the saved version; stale save must preserve the draft.
    assert api.save_strategy(doc['definition'], doc['strategy_id'], 6)['ok']
    strategy_form.locator('[type="submit"]').click()
    page.wait_for_selector('#toast.error:has-text("策略已更新")')
    assert strategy_form.locator('[name="environment"]').input_value() == '保留的未保存编辑'
    page.locator('[data-action="strategy-discard"]').click()
    page.locator('[data-action="strategy-open"]').click()
    page.wait_for_selector('[aria-label="策略版本详情"] h2:has-text("v7")')
    page.locator('[data-action="strategy-edit"]').click()
    strategy_form.locator('[name="environment"]').fill('完成核对后的修改')
    strategy_form.locator('[type="submit"]').click()
    page.wait_for_selector('[aria-label="策略版本详情"] h2:has-text("v8")')
    saved = api.get_strategy(doc['strategy_id'])['data']['definition']
    assert saved['extra_context'] == {'source_version': 'synthetic'}
    assert saved['rules']['entry'][0]['extra_evidence'] == ['synthetic']
    # Externally supplied evidence belongs to the historical confirmed version only.
    run_manifest = {
        'run_id': 'browser-fixture-001', 'stage': 'backtest', 'outcome': 'failed',
        'recorded_by': '离线登记人', 'summary': '<img src=x onerror=alert(1)>合成失败记录',
        'assumptions': ['仅合成数据'], 'limitations': ['未执行真实策略'],
        'implementation': {'entrypoint': 'synthetic:run', 'revision': 'fixture-v1'},
        'parameters': {'window': {'value': 5, 'source': '测试假设'}},
        'data': [{'source': 'synthetic', 'version': 'fixture-v1', 'sha256': 'a' * 64}],
        'period': {'start': '2020-01-01', 'end': '2020-01-31'},
        'started_at': '2026-11-09T10:00:00+08:00',
        'finished_at': '2026-11-09T10:00:02+08:00', 'duration_seconds': 2,
        'metrics': {},
    }
    assert api.record_strategy_run(doc['strategy_id'], 4, run_manifest)['ok']
    evidence = page.locator('[aria-label="运行证据"]')
    page.locator('[data-action="strategy-runs"]').click()
    page.wait_for_selector('[aria-label="运行证据"] p:has-text("本版本尚无登记记录")')
    page.locator('[data-action="strategy-version"][data-version="4"]').click()
    assert '尚未读取本版本证据' in evidence.inner_text()
    page.locator('[data-action="strategy-runs"]').click()
    page.wait_for_selector('[aria-label="运行证据"] h4:has-text("失败")')
    assert run_manifest['summary'] in evidence.inner_text()
    assert evidence.locator('img').count() == 0
    assert '外部记录未核验' in evidence.inner_text()
    evidence.locator('summary').click()
    assert '"value": 5' in evidence.locator('pre').inner_text()
    assert '实现未完成 · 回测未评估 · 样本外未评估' in review.inner_text()
    for width in (760, 1024, 1440, 2560):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        if width == 1440:
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(options.output_dir / 'strategies.png'), full_page=True)
    page.locator('[data-action="strategy-version"][data-version="8"]').click()
    page.set_viewport_size({'width': 1440, 'height': 1000})
    assert confirm.locator('.strategy-ack').evaluate(
        "el => getComputedStyle(el).flexDirection") == 'row'
    page.reload()
    page.wait_for_selector('.page-head')
    page.locator('.sidebar [data-page="strategies"]').click()
    page.wait_for_selector('.strategy-list-item h3:has-text("新一版个人草稿")')
    page.locator('[data-action="strategy-open"]').click()
    page.wait_for_selector('[aria-label="策略版本详情"] h2:has-text("v8")')
    assert page.locator('[data-action="strategy-version"]').count() == 8
    page.locator('[data-action="strategy-version"][data-version="4"]').click()
    page.locator('[data-action="strategy-runs"]').click()
    page.wait_for_selector('[aria-label="运行证据"] h4:has-text("失败")')
    assert not errors, errors
    browser.close()
    print('Browser regression passed: Chinese stock names, legacy holdings, themed dropdown/keyboard, '
          'inline validation, auto reports, responsive layout, persistence '
          'and task/cancel; book registration, duplicates, conflicts and integrity; '
          'strategy drafts, confirmation, versions, conflicts and persistence; no JS errors')
