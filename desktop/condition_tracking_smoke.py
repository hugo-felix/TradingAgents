"""Real API offline acceptance of manual condition tracking and immutable evidence."""
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
    parser.add_argument('--output-dir', type=Path, default=Path('output/condition-tracking'))
    options = parser.parse_args()
    options.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
        store = DesktopStore(Path(directory))
        original, data = fixture(store)
        checks = StrategyCheckRegistry(store)
        definition = copy.deepcopy(original['definition'])
        definition['rules']['entry'][0]['check']['left']['max_age_seconds'] = 86400
        draft = checks.strategies.save(definition)
        strategy = checks.strategies.confirm(draft['strategy_id'], expected_version=1,
                                             confirmed_by='tester')
        checks.strategies.save(definition, strategy_id=strategy['strategy_id'], expected_version=2)
        checks.strategies.confirm(strategy['strategy_id'], expected_version=3, confirmed_by='tester')
        seed = checks.check(strategy['strategy_id'], 2,
                            as_of='2020-01-02T16:00:00+08:00', data=data)
        second = checks.check(strategy['strategy_id'], 2,
                              as_of='2020-01-03T16:00:00+08:00', data=data)
        missing = copy.deepcopy(data)
        missing['series']['close']['observations'] = []
        paused = checks.check(strategy['strategy_id'], 2,
                              as_of='2020-01-04T16:00:00+08:00', data=missing)
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
        conditions = page.locator('[aria-label="策略条件核对"]')
        panel = page.locator('[aria-label="条件跟踪"]')
        evidence = panel.locator('[aria-label="跟踪历史证据"]')

        def choose(check):
            conditions.locator(f'[data-check-id="{check["check_id"]}"]').click()
            panel.locator(':scope > form').wait_for()

        def open_current():
            page.wait_for_selector('.page-head')
            page.locator('.sidebar [data-page="strategies"]').click()
            page.locator(f'[data-action="strategy-open"]'
                         f'[data-strategy="{strategy["strategy_id"]}"]').click()
            page.locator('[data-action="strategy-version"][data-version="2"]').click()
            conditions.locator('[data-condition-action="history"]').click()
            choose(seed)

        def submit():
            panel.locator('[data-tracking="evaluate"]').click()
            panel.locator('[role="status"]').wait_for(state='detached')

        page.goto(Path('tradingagents/desktop/static/index.html').resolve().as_uri())
        open_current()
        assert api.list_condition_tracking()['data'] == []
        panel.locator('[name="condition"]').select_option('entry:0')
        panel.locator('[name="expiry"]').fill('2020-01-08T16:00:00')
        panel.locator('[name="by"]').fill('<img src=x onerror=alert(1)> 测试人')
        panel.locator(':scope > form [type="submit"]').click()
        panel.locator('[role="alert"]').wait_for()
        assert api.list_condition_tracking()['data'] == []
        assert panel.locator('[name="by"]').input_value().endswith('测试人')
        panel.locator('[name="expiry"]').fill('2020-01-08T16:00:00+08:00')
        # Pending creation must recover into the correct version after navigation.
        page.evaluate("""() => {
          const original=window.pywebview.api.create_condition_tracking;
          window.pywebview.api.create_condition_tracking=(...args)=>new Promise(resolve=>{
            window.releaseTracking=async()=>{
              window.pywebview.api.create_condition_tracking=original;
              resolve(await original(...args));
            };
          });
        }""")
        panel.locator(':scope > form [type="submit"]').click()
        panel.locator('[role="status"]').wait_for()
        page.locator('[data-action="strategy-version"][data-version="4"]').click()
        assert panel.locator('form').count() == 0
        panel.locator('[data-tracking="plans"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        assert panel.locator('[data-plan-id]').count() == 0
        page.locator('[data-action="strategy-version"][data-version="2"]').click()
        assert panel.locator(':scope > form [type="submit"]').is_disabled()
        page.evaluate('window.releaseTracking()')
        panel.locator('[aria-label="所选跟踪计划"]').wait_for()
        assert panel.locator('script,img').count() == 0
        plans = api.list_condition_tracking()['data']
        assert len(plans) == 1
        plan_id = plans[0]['plan_id']
        assert api.condition_tracking_history(plan_id)['data'] == []
        panel.locator(':scope > form [type="submit"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        assert len(api.list_condition_tracking()['data']) == 1
        submit()
        assert '连续 1/2' in evidence.inner_text()
        panel.locator('[data-tracking="history"]').click()
        panel.locator('[aria-label="最近跟踪状态"]').wait_for()
        choose(second)
        submit()
        assert '本次达到阈值' in evidence.inner_text()
        assert '本次命中' in evidence.inner_text()
        choose(seed)
        submit()
        assert '连续 1/2' in evidence.inner_text()
        assert '2020-01-03' in panel.locator('[aria-label="最近跟踪状态"]').inner_text()
        assert len(api.condition_tracking_history(plan_id)['data']) == 2
        choose(paused)
        submit()
        assert '暂停判断' in evidence.inner_text()
        assert '连续 0/2' in evidence.inner_text()
        assert '数据不足' in evidence.inner_text()
        assert evidence.locator('script,img').count() == 0
        page.evaluate("""() => {
          const original=window.pywebview.api.condition_tracking_history;
          window.pywebview.api.condition_tracking_history=()=>Promise.resolve(
            {ok:false,error:'离线读取失败'});
          window.restoreTracking=()=>{window.pywebview.api.condition_tracking_history=original;};
        }""")
        panel.locator('[data-tracking="history"]').click()
        panel.locator('[role="alert"]:has-text("离线读取失败")').wait_for()
        assert '暂停判断' in evidence.inner_text()
        assert panel.locator('[data-event-id]').count() == 3
        page.evaluate('window.restoreTracking()')
        panel.locator('[data-tracking="history"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        stop = panel.locator('[data-tracking-stop]')
        stop.locator('[name="stopBy"]').fill('具名测试人')
        stop.locator('[name="stopReason"]').fill('<img src=x onerror=alert(1)> 策略已替换')
        page.evaluate("""() => {
          const original=window.pywebview.api.cancel_condition_tracking;
          window.pywebview.api.cancel_condition_tracking=()=>Promise.resolve(
            {ok:false,error:'停止保存失败'});
          window.restoreStop=()=>{window.pywebview.api.cancel_condition_tracking=original;};
        }""")
        stop.locator('[type="submit"]').click()
        panel.locator('[role="alert"]:has-text("停止保存失败")').wait_for()
        assert stop.locator('[name="stopReason"]').input_value().endswith('策略已替换')
        assert 'cancellation' not in api.get_condition_tracking(plan_id)['data']
        page.evaluate('window.restoreStop()')
        page.evaluate("""() => {
          const original=window.pywebview.api.cancel_condition_tracking;
          window.pywebview.api.cancel_condition_tracking=(...args)=>new Promise(resolve=>{
            window.releaseStop=async()=>{
              window.pywebview.api.cancel_condition_tracking=original;
              resolve(await original(...args));
            };
          });
        }""")
        stop.locator('[type="submit"]').click()
        panel.locator('[role="status"]').wait_for()
        page.locator('[data-action="strategy-version"][data-version="4"]').click()
        assert panel.locator('[aria-label="停止跟踪记录"]').count() == 0
        page.locator('[data-action="strategy-version"][data-version="2"]').click()
        assert stop.locator('[type="submit"]').is_disabled()
        page.evaluate('window.releaseStop()')
        panel.locator('[aria-label="停止跟踪记录"]').wait_for()
        assert '策略已替换' in panel.locator('[aria-label="停止跟踪记录"]').inner_text()
        assert panel.locator('[data-tracking="evaluate"]').is_disabled()
        assert panel.locator('[data-tracking-stop]').count() == 0
        assert panel.locator('img,script').count() == 0
        assert len(api.condition_tracking_history(plan_id)['data']) == 3
        # Same creation identity must preserve the stopped state rather than reactivate it.
        choose(seed)
        panel.locator(':scope > form [type="submit"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        panel.locator('[aria-label="停止跟踪记录"]').wait_for()
        assert len(api.list_condition_tracking()['data']) == 1
        panel.locator('[data-tracking="history"]').click()
        panel.locator('[data-event-id]').first.click()
        for width in (760, 1024, 1440, 2560):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.set_viewport_size({'width': 1440, 'height': 1000})
        panel.screenshot(path=str(options.output_dir / 'tracking.png'))
        page.reload()
        open_current()
        assert panel.locator('[aria-label="所选跟踪计划"]').count() == 0
        panel.locator('[data-tracking="plans"]').click()
        panel.locator(f'[data-plan-id="{plan_id}"]').click()
        panel.locator('[aria-label="停止跟踪记录"]').wait_for()
        assert panel.locator('[data-tracking="evaluate"]').is_disabled()
        panel.locator('[data-tracking="history"]').click()
        panel.locator('[data-event-id]').first.click()
        assert '暂停判断' in evidence.inner_text()
        assert '连续 0/2' in panel.locator('[aria-label="最近跟踪状态"]').inner_text()
        assert not errors, errors
        print('Condition tracking passed: real API, explicit creation, validation, version isolation, '
              'independent observations, idempotency, pause, evidence, escaping, failure retry, '
              'stop audit and retry, pending navigation, reload and four widths.')


if __name__ == '__main__':
    main()
