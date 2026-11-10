"""Real API, offline acceptance of independent strategies and personal conflict evidence."""
from __future__ import annotations

import argparse
import copy
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path

from playwright.sync_api import sync_playwright
from strategy_conditions_smoke import fixture

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategy_checks import StrategyCheckRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium-path')
    parser.add_argument('--output-dir', type=Path, default=Path('output/strategy-comparisons'))
    options = parser.parse_args()
    options.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
        store = DesktopStore(Path(directory))
        first, data = fixture(store)
        checks = StrategyCheckRegistry(store)
        definition = copy.deepcopy(first['definition'])
        definition['name'] = '<script>价值测试假设</script>'
        draft = checks.strategies.save(definition)
        other = checks.strategies.confirm(draft['strategy_id'], expected_version=1,
                                          confirmed_by='tester')
        cutoff = '2020-01-02T16:00:00+08:00'
        left = checks.check(first['strategy_id'], 2, as_of=cutoff, data=data)
        data['symbol'] = 'OTHER'
        data['source'] = '<img src=x onerror=alert(1)>'
        right = checks.check(other['strategy_id'], 2, as_of=cutoff, data=data)
        api = DesktopAPI(store)
        cleanup.callback(api._tasks.close)
        playwright = cleanup.enter_context(sync_playwright())
        browser = playwright.chromium.launch(executable_path=options.chromium_path)
        cleanup.callback(browser.close)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        methods = [name for name in dir(api)
                   if not name.startswith('_') and callable(getattr(api, name))]
        page.expose_function('desktopCall', lambda method, args: getattr(api, method)(*args))
        page.add_init_script('window.pywebview={api:Object.fromEntries(' + json.dumps(methods) +
                             '.map(name=>[name,(...args)=>window.desktopCall(name,args)]))};')

        def open_current():
            page.wait_for_selector('.page-head')
            page.locator('.sidebar [data-page="strategies"]').click()
            page.locator(f'[data-action="strategy-open"][data-strategy="{first["strategy_id"]}"]').click()
            page.locator('[data-action="strategy-version"][data-version="2"]').click()
            conditions.locator('[data-condition-action="history"]').click()
            conditions.locator(f'[data-check-id="{left["check_id"]}"]').click()
            panel.locator('form').wait_for()

        page.goto(Path('tradingagents/desktop/static/index.html').resolve().as_uri())
        conditions = page.locator('[aria-label="策略条件核对"]')
        panel = page.locator('[aria-label="策略独立对照"]')
        open_current()
        panel.locator('[name="otherId"]').fill(left['check_id'])
        panel.locator('[data-compare="preview"]').click()
        panel.locator('[role="alert"]:has-text("两个不同")').wait_for()
        panel.locator('[name="otherId"]').fill('missing')
        panel.locator('[data-compare="preview"]').click()
        panel.locator('[role="alert"]:has-text("未找到")').wait_for()
        panel.locator('[name="otherId"]').fill(right['check_id'])
        panel.locator('[data-compare="preview"]').click()
        preview = panel.locator('[aria-label="待保存对照证据"]')
        preview.wait_for()
        assert '上下文存在差异' in preview.inner_text()
        assert '证券 / 数据来源' in preview.inner_text()
        assert preview.locator('script,img').count() == 0
        assert preview.locator('[aria-label="条件检查快照"]').count() == 2
        assert api.strategy_comparison_history(left['check_id'])['data'] == []
        panel.locator('[name="verdict"]').select_option('conflict_identified')
        panel.locator('[name="by"]').fill('测试人')
        panel.locator('[type="submit"]').click()
        panel.locator('[role="alert"]:has-text("依据及适用条件")').wait_for()
        panel.locator('[name="note"]').fill('<img src=x onerror=alert(1)> 证券与来源不同，保留个人分歧说明。')
        # A successful write followed by a pending navigation must remain recoverable.
        page.evaluate("""() => {
          const original=window.pywebview.api.save_strategy_comparison;
          window.pywebview.api.save_strategy_comparison=(...args)=>new Promise(resolve=>{
            window.releaseComparison=async()=>{
              window.pywebview.api.save_strategy_comparison=original;
              resolve(await original(...args));
            };
          });
        }""")
        panel.locator('[type="submit"]').click()
        panel.locator('[role="status"]').wait_for()
        page.locator('[data-action="strategy-version"][data-version="4"]').click()
        assert panel.locator('form').count() == 0
        page.locator('[data-action="strategy-version"][data-version="2"]').click()
        assert panel.locator('[type="submit"]').is_disabled()
        page.evaluate('window.releaseComparison()')
        saved = panel.locator('[aria-label="已保存策略对照"]')
        saved.wait_for()
        assert saved.locator('script,img').count() == 0
        assert '人工识别到分歧 · 测试人' in saved.inner_text()
        assert '上下文存在差异' in saved.inner_text()
        assert len(api.strategy_comparison_history(right['check_id'])['data']) == 1
        panel.locator('[type="submit"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        assert len(api.strategy_comparison_history(left['check_id'])['data']) == 1
        page.evaluate("""() => {
          const original=window.pywebview.api.strategy_comparison_history;
          window.pywebview.api.strategy_comparison_history=()=>Promise.resolve({ok:false,error:'离线读取失败'});
          window.restoreComparisons=()=>{window.pywebview.api.strategy_comparison_history=original;};
        }""")
        panel.locator('[data-compare="history"]').click()
        panel.locator('[role="alert"]:has-text("离线读取失败")').wait_for()
        assert saved.is_visible()
        assert '<img' in panel.locator('[name="note"]').input_value()
        page.evaluate('window.restoreComparisons()')
        panel.locator('[data-compare="history"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        for width in (760, 1024, 1440, 2560):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.set_viewport_size({'width': 1440, 'height': 1000})
        saved.screenshot(path=str(options.output_dir / 'comparison.png'))
        page.reload()
        open_current()
        assert not saved.count()
        panel.locator('[data-compare="history"]').click()
        panel.locator('[data-comparison-id]').click()
        saved.wait_for()
        assert '人工识别到分歧 · 测试人' in saved.inner_text()
        assert not errors, errors
        print('Strategy comparisons passed: real API, context differences, independent evidence, '
              'escaping, validation, retry, idempotency, pending navigation, reload and four widths.')


if __name__ == '__main__':
    main()
