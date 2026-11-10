"""Offline browser acceptance of timing evidence through the real desktop API."""
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
from tradingagents.desktop.strategy_checks import StrategyCheckRegistry


def fixture(store):
    registry = StrategyRegistry(store)
    definition = {**dict.fromkeys(SCOPE_FIELDS, '合成离线验收'),
                  'market': 'CN-A', 'timeframe': 'daily', 'rules': {
                      key: [{'text': '人工核对', 'citation': '测试假设',
                             'kind': 'personal', 'state': 'manual'}] for key in RULE_FIELDS},
                  'timing': {'observation': 'completed_bar', 'confirmation_delay_seconds': 60,
                             'execution': 'next_session_open', 'price_basis': 'raw',
                             'price_unit': 'CNY/share'}}
    definition['rules']['entry'][0].update(state='defined', check={
        'op': 'gt', 'left': {'metric': 'close'},
        'right': {'constant': 9, 'unit': 'CNY/share', 'source': '测试假设'}})
    draft = registry.save(definition)
    first = registry.confirm(draft['strategy_id'], expected_version=1, confirmed_by='tester')
    without_timing = copy.deepcopy(definition)
    without_timing.pop('timing')
    registry.save(without_timing, strategy_id=first['strategy_id'], expected_version=2)
    registry.confirm(first['strategy_id'], expected_version=3, confirmed_by='tester')
    data = {'symbol': 'SYNTHETIC', 'market': 'CN-A', 'timeframe': 'daily',
            'price_basis': 'raw', 'source': 'synthetic', 'revision': 'fixture-v1',
            'series': {'close': {'unit': 'CNY/share', 'observations': [
                {'observed_at': '2020-01-03T15:00:00+08:00',
                 'available_at': '2020-01-03T15:05:00+08:00', 'value': 10}]}}}
    checks = StrategyCheckRegistry(store)
    check = checks.check(first['strategy_id'], 2, as_of='2020-01-03T16:00:00+08:00',
                         data=data)
    other = checks.check(first['strategy_id'], 2, as_of='2020-01-03T17:00:00+08:00',
                         data=data)
    evidence = {'calendar': {'market': 'CN-A', 'source': '<script>合成日历</script>',
                             'revision': 'v1', 'complete': True,
                             'coverage_start': '2020-01-03T00:00:00+08:00',
                             'coverage_end': '2020-01-07T00:00:00+08:00', 'sessions': [
                                 {'open_at': '2020-01-03T09:30:00+08:00',
                                  'close_at': '2020-01-03T15:00:00+08:00'},
                                 {'open_at': '2020-01-06T09:30:00+08:00',
                                  'close_at': '2020-01-06T15:00:00+08:00'}]},
                'execution_price': {'at': '2020-01-06T09:30:00+08:00',
                                    'available_at': '2020-01-06T09:30:01+08:00',
                                    'value': 11, 'unit': 'CNY/share', 'price_basis': 'raw',
                                    'source': 'synthetic quote', 'revision': 'v1'}}
    return first, check, other, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium-path')
    parser.add_argument('--output-dir', type=Path, default=Path('output/strategy-timing'))
    options = parser.parse_args()
    options.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
        store = DesktopStore(Path(directory))
        first, check, other, evidence = fixture(store)
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

        def open_check():
            page.wait_for_selector('.page-head')
            page.locator('.sidebar [data-page="strategies"]').click()
            page.locator('[data-action="strategy-open"]').click()
            page.locator('[data-action="strategy-version"][data-version="2"]').click()
            page.locator('[data-condition-action="history"]').click()
            page.locator(f'[data-check-id="{check["check_id"]}"]').click()

        open_check()
        panel = page.locator('[aria-label="策略时序审计"]')
        field = panel.locator('[name="timing_evidence"]')
        submit = panel.locator('[type="submit"]')
        result = panel.locator('[aria-label="时序审计快照"]')
        field.fill('{invalid')
        submit.click()
        panel.locator('[role="alert"]:has-text("有效 JSON")').wait_for()
        assert field.input_value() == '{invalid'
        assert api.strategy_timing_history(check['check_id'])['data'] == []
        field.fill(json.dumps(evidence, ensure_ascii=False))
        submit.click()
        result.locator('h4:has-text("时序一致")').wait_for()
        assert '2020-01-06T09:30:00+08:00' in result.inner_text()
        assert result.locator('script,img').count() == 0
        submit.click()
        panel.locator('[role="status"]').wait_for(state='detached')
        assert len(api.strategy_timing_history(check['check_id'])['data']) == 1
        missing = copy.deepcopy(evidence)
        missing['execution_price'] = None
        field.fill(json.dumps(missing))
        submit.click()
        result.locator('h4:has-text("证据不足")').wait_for()
        conflict = copy.deepcopy(evidence)
        conflict['execution_price']['price_basis'] = 'adjusted'
        field.fill(json.dumps(conflict))
        submit.click()
        result.locator('h4:has-text("时序不一致")').wait_for()
        assert len(api.strategy_timing_history(check['check_id'])['data']) == 3
        assert api.get_strategy_check(check['check_id'])['data'] == check
        # Leave and return while an API call is pending; the remounted view must finish.
        page.evaluate("""() => {
          const original=window.pywebview.api.assess_strategy_timing;
          window.pywebview.api.assess_strategy_timing=(...args)=>new Promise(resolve=>{
            window.releaseTiming=async()=>{
              window.pywebview.api.assess_strategy_timing=original;
              resolve(await original(...args));
            };
          });
        }""")
        submit.click()
        panel.locator('[role="status"]').wait_for()
        page.locator('[data-action="strategy-version"][data-version="4"]').click()
        page.locator('[data-action="strategy-version"][data-version="2"]').click()
        assert submit.is_disabled()
        page.evaluate('window.releaseTiming()')
        panel.locator('[role="status"]').wait_for(state='detached')
        assert submit.is_enabled()
        page.locator(f'[data-check-id="{other["check_id"]}"]').click()
        panel.locator('[data-timing-history]').click()
        panel.locator('p:has-text("本次检查尚无时序审计")').wait_for()
        assert field.input_value() == ''
        page.locator(f'[data-check-id="{check["check_id"]}"]').click()
        assert field.input_value() == json.dumps(conflict)
        page.evaluate("""() => {
          window.originalTimingHistory=window.pywebview.api.strategy_timing_history;
          window.pywebview.api.strategy_timing_history=async()=>({ok:false,error:'synthetic failure'});
        }""")
        panel.locator('[data-timing-history]').click()
        panel.locator('[role="alert"]:has-text("synthetic failure")').wait_for()
        assert '时序不一致' in result.inner_text()
        page.evaluate('window.pywebview.api.strategy_timing_history=window.originalTimingHistory')
        panel.locator('[data-timing-history]').click()
        panel.locator('[role="alert"]').wait_for(state='detached')
        for width in (760, 1024, 1440, 2560):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        panel.screenshot(path=str(options.output_dir / 'timing.png'))
        page.reload()
        open_check()
        panel.locator('[data-timing-history]').click()
        rows = api.strategy_timing_history(check['check_id'])['data']
        panel.locator(f'[data-audit-id="{rows[-1]["audit_id"]}"]').click()
        result.locator('h4:has-text("时序一致")').wait_for()
        page.locator('[data-action="strategy-version"][data-version="4"]').click()
        assert panel.count() == 0
        assert api.strategy_check_history(first['strategy_id'], 4)['data'] == []
        # An old strategy without a contract may still have conditions, but no audit form.
        raw = copy.deepcopy(check['data'])
        # Check snapshots retain the full caller data under data.
        fourth = api.check_strategy_conditions(first['strategy_id'], 4, check['as_of'], raw)
        assert fourth['ok'], fourth
        page.locator('[data-condition-action="history"]').click()
        page.locator(f'[data-check-id="{fourth["data"]["check_id"]}"]').click()
        assert '本版本未定义时序契约' in panel.inner_text()
        assert panel.locator('form').count() == 0
        assert not errors, errors
        print('Timing browser acceptance passed: real API, three states, immutable checks, '
              'idempotency, escaping, pending navigation, retry, four widths, '
              'check/version isolation and reload.')


if __name__ == '__main__':
    main()
