"""Offline real-API acceptance of append-only timing contract editing."""
from __future__ import annotations

import argparse
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path

from playwright.sync_api import sync_playwright
from strategy_timing_smoke import fixture

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium-path')
    options = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
        store = DesktopStore(Path(directory))
        first, _, _, _ = fixture(store)
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
        page.locator('.sidebar [data-page="strategies"]').click()

        def reopen(version):
            page.locator('[data-action="strategies-reload"]').click()
            page.locator('[data-action="strategy-open"]').click()
            page.locator(f'[data-version="{version}"]:disabled').wait_for()

        reopen(4)
        panel = page.locator('[aria-label="时序契约编辑"]')
        page.locator('[data-action="strategy-edit"]').click()
        assert panel.locator('form').count() == 0
        page.locator('[data-action="strategy-discard"]').click()
        enabled = panel.locator('[name="enabled"]')
        enabled.check()
        panel.locator('[name="delay"]').fill('120')
        panel.locator('[name="basis"]').fill('raw')
        panel.locator('[name="unit"]').fill('CNY/share')
        page.locator('[data-version="2"]').click()
        assert panel.locator('[name="delay"]').input_value() == '60'
        page.locator('[data-version="4"]').click()
        assert panel.locator('[name="delay"]').input_value() == '120'
        page.evaluate('''() => {
          window.savedSave=window.pywebview.api.save_strategy;
          window.pywebview.api.save_strategy=async()=>({ok:false,error:'synthetic failure'});
        }''')
        panel.locator('[type="submit"]').click()
        panel.locator('[role="alert"]:has-text("synthetic failure")').wait_for()
        assert panel.locator('[name="delay"]').input_value() == '120'
        page.evaluate('window.pywebview.api.save_strategy=window.savedSave')
        page.evaluate('''() => {
          window.pywebview.api.save_strategy=(...args)=>new Promise(resolve=>{
            window.releaseTimingSave=()=>resolve(window.savedSave(...args));
          });
        }''')
        panel.locator('[type="submit"]').click()
        page.locator('[data-version="2"]').click()
        page.evaluate('window.releaseTimingSave()')
        page.wait_for_function('''() => document.querySelector('[data-version="2"]:disabled')''')
        assert panel.locator('[name="delay"]').input_value() == '60'
        page.locator('[data-version="4"]').click()
        panel.locator('[role="status"]:has-text("v5")').wait_for()
        page.evaluate('window.pywebview.api.save_strategy=window.savedSave')
        draft = api.get_strategy(first['strategy_id'])['data']
        assert draft['status'] == 'draft' and draft['version'] == 5
        assert draft['definition']['timing']['confirmation_delay_seconds'] == 120
        assert api.get_strategy(first['strategy_id'], 2)['data'] == first
        reopen(5)
        confirm = page.locator('#strategy-confirm-form')
        confirm.locator('[name="confirmed_by"]').fill('离线测试')
        confirm.locator('[type="checkbox"]').check()
        confirm.locator('[type="submit"]').click()
        page.locator('[data-version="6"]:disabled').wait_for()
        confirmed = api.get_strategy(first['strategy_id'])['data']
        assert confirmed['status'] == 'confirmed'
        assert confirmed['definition'] == draft['definition']
        panel.locator('[type="submit"]').click()
        panel.locator('[role="alert"]:has-text("未修改")').wait_for()
        assert api.get_strategy(first['strategy_id'])['data']['version'] == 6
        panel.locator('[name="delay"]').fill('180')
        concurrent = api.save_strategy(confirmed['definition'], first['strategy_id'], 6)
        assert concurrent['ok'], concurrent
        panel.locator('[type="submit"]').click()
        panel.locator('[role="alert"]:has-text("版本")').wait_for()
        assert panel.locator('[name="delay"]').input_value() == '180'
        assert api.get_strategy(first['strategy_id'])['data']['version'] == 7
        reopen(7)
        enabled.uncheck()
        assert panel.locator('[name="delay"]').is_disabled()
        panel.locator('[type="submit"]').click()
        panel.locator('[role="status"]:has-text("v8")').wait_for()
        removed = api.get_strategy(first['strategy_id'])['data']
        assert removed['status'] == 'draft' and 'timing' not in removed['definition']
        assert api.get_strategy(first['strategy_id'], 6)['data'] == confirmed
        for width in (760, 1024, 1440, 2560):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.reload()
        page.locator('.sidebar [data-page="strategies"]').click()
        reopen(8)
        assert not enabled.is_checked()
        assert not errors, errors
        print('Timing editor passed: append-only save, explicit confirmation, removal, '
              'unchanged guard, failure retry, version isolation, conflict and reload.')


if __name__ == '__main__':
    main()
