"""Offline browser acceptance of version-bound condition evidence through the real API."""
from __future__ import annotations

import argparse
import copy
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path

from playwright.sync_api import sync_playwright

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import RULE_FIELDS, SCOPE_FIELDS, StrategyRegistry


def fixture(store):
    registry = StrategyRegistry(store)
    definition = {**dict.fromkeys(SCOPE_FIELDS, '合成离线验收'),
                  'market': 'synthetic', 'timeframe': 'daily', 'rules': {
                      key: [{'text': '人工核对', 'citation': '测试假设',
                             'kind': 'personal', 'state': 'manual'}] for key in RULE_FIELDS}}
    expr = {'op': 'gt', 'left': {'metric': 'close'},
            'right': {'constant': 20, 'unit': 'CNY/share', 'source': '测试假设'}}
    rules = []
    for op in ('gt', 'lt', 'unsupported', 'ge', 'eq'):
        check = copy.deepcopy(expr)
        check['op'] = op
        if op == 'ge':
            check['left']['metric'] = 'missing'
        if op == 'eq':
            check['right']['unit'] = 'USD/share'
        rules.append({'text': '<img src=x onerror=alert(1)> 原样规则',
                      'citation': '<script>测试出处</script>', 'kind': 'personal',
                      'state': 'defined', 'check': check})
    definition['rules']['entry'] = rules
    draft = registry.save(definition)
    first = registry.confirm(draft['strategy_id'], expected_version=1, confirmed_by='tester')
    registry.save(definition, strategy_id=first['strategy_id'], expected_version=2)
    registry.confirm(first['strategy_id'], expected_version=3, confirmed_by='tester')
    data = {'symbol': 'SYNTHETIC', 'market': 'synthetic', 'timeframe': 'daily',
            'price_basis': 'raw', 'source': '<script>合成来源</script>', 'revision': 'fixture-v1',
            'series': {'close': {'unit': 'CNY/share', 'observations': [
                {'observed_at': '2020-01-02T15:00:00+08:00',
                 'available_at': '2020-01-02T15:05:00+08:00', 'value': 30},
                {'observed_at': '2020-01-03T15:00:00+08:00',
                 'available_at': '2020-01-03T15:05:00+08:00', 'value': 999}]}}}
    return first, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium-path')
    parser.add_argument('--output-dir', type=Path, default=Path('output/strategy-conditions'))
    options = parser.parse_args()
    options.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
        store = DesktopStore(Path(directory))
        first, data = fixture(store)
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
        page.goto(Path('tradingagents/desktop/static/index.html').resolve().as_uri())
        page.wait_for_selector('.page-head')
        page.locator('.sidebar [data-page="strategies"]').click()
        page.locator('[data-action="strategy-open"]').click()
        panel = page.locator('[aria-label="策略条件核对"]')
        panel.locator('h3:has-text("v4")').wait_for()
        page.locator('[data-action="strategy-version"][data-version="2"]').click()
        panel.locator('[name="as_of"]').fill('2020-01-02T16:00:00+08:00')
        panel.locator('[name="data"]').fill('{invalid')
        panel.locator('[type="submit"]').click()
        panel.locator('[role="alert"]:has-text("有效 JSON")').wait_for()
        assert panel.locator('[name="data"]').input_value() == '{invalid'
        assert api.strategy_check_history(first['strategy_id'], 2)['data'] == []
        panel.locator('[name="data"]').fill(json.dumps(data, ensure_ascii=False))
        panel.locator('[type="submit"]').click()
        result = panel.locator('[aria-label="条件检查快照"]')
        result.locator('h4:has-text("SYNTHETIC")').wait_for()
        for status in ('满足', '未满足', '需人工判断', '数据不足', '未实现', '定义校验失败'):
            assert status in result.inner_text(), status
        assert result.locator('script,img').count() == 0
        assert '30 CNY/share > 20 CNY/share' in result.inner_text()
        assert '调用者提供' in result.inner_text()
        result.locator('summary:has-text("左侧计算值")').first.click()
        assert '2020-01-02T15:05:00+08:00' in result.inner_text()
        assert '999' not in result.locator('article').first.inner_text()
        rows = api.strategy_check_history(first['strategy_id'], 2)['data']
        assert len(rows) == 1
        check_id = rows[0]['check_id']
        panel.locator('[type="submit"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        assert len(api.strategy_check_history(first['strategy_id'], 2)['data']) == 1
        # Re-rendering the main view while a check is pending must release its busy state.
        page.evaluate("""() => {
          const original=window.pywebview.api.check_strategy_conditions;
          window.pywebview.api.check_strategy_conditions=(...args)=>new Promise(resolve=>{
            window.releaseCheck=async()=>resolve(await original(...args));
          });
        }""")
        panel.locator('[type="submit"]').click()
        panel.locator('[role="status"]').wait_for()
        page.locator('[data-action="strategy-version"][data-version="4"]').click()
        assert '尚未选择检查快照' in panel.inner_text()
        page.locator('[data-action="strategy-version"][data-version="2"]').click()
        assert panel.locator('[type="submit"]').is_disabled()
        page.evaluate('window.releaseCheck()')
        panel.locator('[role="status"]').wait_for(state='detached')
        assert panel.locator('[type="submit"]').is_enabled()
        # A failed historical read keeps the current result and supports retry.
        page.evaluate("""() => {
          const original=window.pywebview.api.strategy_check_history;
          window.pywebview.api.strategy_check_history=()=>Promise.resolve({ok:false,error:'离线读取失败'});
          window.restoreHistory=()=>{window.pywebview.api.strategy_check_history=original;};
        }""")
        panel.locator('[data-condition-action="history"]').click()
        panel.locator('[role="alert"]:has-text("离线读取失败")').wait_for()
        assert result.locator('h4:has-text("SYNTHETIC")').count() == 1
        page.evaluate('window.restoreHistory()')
        panel.locator('[data-condition-action="history"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        assert panel.locator('[role="alert"]').count() == 0
        for width in (760, 1024, 1440, 2560):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.set_viewport_size({'width': 1440, 'height': 1000})
        panel.screenshot(path=str(options.output_dir / 'conditions.png'))
        page.reload()
        page.wait_for_selector('.page-head')
        page.locator('.sidebar [data-page="strategies"]').click()
        page.locator('[data-action="strategy-open"]').click()
        panel.locator('[data-condition-action="history"]').click()
        panel.locator('p:has-text("本版本尚无检查记录")').wait_for()
        page.locator('[data-action="strategy-version"][data-version="2"]').click()
        panel.locator('[data-condition-action="history"]').click()
        panel.locator(f'[data-check-id="{check_id}"]').click()
        result.locator('h4:has-text("SYNTHETIC")').wait_for()
        page.locator('[data-action="strategy-version"][data-version="1"]').click()
        assert panel.locator('form').count() == 0
        assert '草稿尚未确认' in panel.inner_text()
        assert not errors, errors
        print('Conditions browser acceptance passed: real API, six states, PIT evidence, escaping, '
              'idempotency, pending navigation, retry, four widths, version isolation and reload.')


if __name__ == '__main__':
    main()
