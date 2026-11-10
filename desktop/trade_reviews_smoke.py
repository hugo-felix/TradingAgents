"""Offline browser acceptance of personal reviews through the real desktop API."""
from __future__ import annotations

import argparse
import csv
import io
import json
import tempfile
from contextlib import ExitStack
from pathlib import Path

from playwright.sync_api import sync_playwright

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.reviews import ReviewRegistry
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import StrategyRegistry
from tradingagents.desktop.trade_contexts import TradeContextRegistry
from tradingagents.desktop.trades import COLUMNS, TradeLedger


def fixture(store):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, COLUMNS)
    writer.writeheader()
    for ordinal in range(21):
        writer.writerow({'broker': 'synthetic', 'account_ref': 'synthetic',
                         'execution_id': str(ordinal), 'trade_date': '2024-01-02',
                         'executed_at': '2024-01-02T10:15:00+08:00', 'ticker': '600941',
                         'market': 'CN-A', 'side': 'buy', 'quantity': '100',
                         'price': '65', 'currency': 'CNY', 'fees': ''})
    ledger = TradeLedger(store)
    text = output.getvalue()
    ledger.import_csv(text, 'synthetic.csv', ledger.preview(text, 'synthetic.csv')['sha256'])
    trade = ledger.list_fills()['items'][0]
    TradeContextRegistry(store).record(trade['trade_id'], {'personal_reason': '原始说明'})
    return trade['trade_id']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium-path')
    parser.add_argument('--output-dir', type=Path, default=Path('output/trade-reviews'))
    options = parser.parse_args()
    options.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
        store = DesktopStore(Path(directory))
        trade_id = fixture(store)
        registry = ReviewRegistry(store)
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
        page.locator('.sidebar [data-page="reviews"]').click()
        panel = page.locator('[data-trade-reviews]')
        panel.locator('[data-review-trade]').first.wait_for()
        assert panel.locator('[data-review-trade]').count() == 20
        panel.locator('[data-review-action="fills-next"]').click()
        panel.locator('[data-review-action="fills-prev"]:enabled').wait_for()
        assert panel.locator('[data-review-trade]').count() == 1
        panel.locator('[data-review-action="fills-prev"]').click()
        panel.locator(f'[data-review-trade="{trade_id}"]').click()
        record = panel.locator('[data-review-form="record"]')
        record.locator('button').click()
        panel.locator('[role="alert"]').wait_for()
        assert registry.list(trade_id)['total'] == 0
        uncertainty = '<img src=x onerror=alert(1)> 单笔结果不能建立因果'
        record.locator('[name="uncertainty"]').fill(uncertainty)
        # A committed write with a lost response must be retried with the same request ID.
        page.evaluate("""() => {
          const original=window.pywebview.api.record_trade_review;
          window.pywebview.api.record_trade_review=async(...args)=>{
            await original(...args);
            window.pywebview.api.record_trade_review=original;
            return {ok:false,error:'合成响应中断'};
          };
        }""")
        record.locator('button').click()
        panel.locator('[role="alert"]:has-text("合成响应中断")').wait_for()
        assert registry.list(trade_id)['total'] == 1
        assert record.locator('[name="uncertainty"]').input_value() == uncertainty
        record.locator('button').click()
        result = panel.locator('[aria-label="复盘快照"]')
        result.wait_for()
        assert registry.list(trade_id)['total'] == 1
        original = registry.list(trade_id)['items'][0]
        assert original['notes'] == dict.fromkeys(('research', 'strategy', 'execution')) | {
            'uncertainty': uncertainty}
        assert result.locator('dd:has-text("尚未评估")').count() == 3
        assert result.locator('img,script').count() == 0
        assert record.locator('button').is_disabled()
        TradeContextRegistry(store).record(trade_id, {'personal_reason': '后来补录'},
                                           expected_version=1)
        assert registry.get(original['review_id']) == original
        # Returning to a form while its request is pending preserves input and releases busy state.
        record.locator('[name="research"]').fill('尚不足以评价方向')
        page.evaluate("""() => {
          const original=window.pywebview.api.record_trade_review;
          window.pywebview.api.record_trade_review=(...args)=>new Promise(resolve=>{
            window.releaseReview=async()=>{
              window.pywebview.api.record_trade_review=original;
              resolve(await original(...args));
            };
          });
        }""")
        record.locator('button').click()
        panel.locator('[role="status"]').wait_for()
        page.locator('.sidebar [data-page="today"]').click()
        page.locator('.sidebar [data-page="reviews"]').click()
        assert record.locator('button').is_disabled()
        page.evaluate('window.releaseReview()')
        panel.locator('[role="status"]').wait_for(state='detached')
        assert registry.list(trade_id)['total'] == 2
        assert record.locator('[name="research"]').input_value() == '尚不足以评价方向'
        # Reverting known-saved text is a new explicit assessment, not an old retry.
        record.locator('[name="research"]').fill('')
        page.evaluate("""() => {
          const original=window.pywebview.api.list_trade_reviews;
          window.pywebview.api.list_trade_reviews=()=>Promise.resolve({ok:false,error:'合成历史失败'});
          window.restoreHistory=()=>{window.pywebview.api.list_trade_reviews=original;};
        }""")
        record.locator('button').click()
        panel.locator('[role="alert"]:has-text("合成历史失败")').wait_for()
        assert registry.list(trade_id)['total'] == 3
        assert record.locator('button').is_disabled()
        latest = registry.list(trade_id)['items'][0]
        assert latest['review_id'] in result.inner_text()
        page.evaluate('window.restoreHistory()')
        panel.locator('[data-review-action="refresh"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        # Rule input is isolated by review, even on the same trade.
        rule = panel.locator('[data-review-form="rule"]')
        rule.locator('[name="name"]').fill('最新复盘草案')
        panel.locator(f'[data-review-id="{original["review_id"]}"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        assert rule.locator('[name="name"]').input_value() == ''
        panel.locator(f'[data-review-id="{latest["review_id"]}"]').click()
        panel.locator('[role="status"]').wait_for(state='detached')
        assert rule.locator('[name="name"]').input_value() == '最新复盘草案'
        rule.locator('[name="text"]').fill('等待证据后再评估')
        rule.locator('[name="citation"]').fill('个人假设，尚未验证')
        for width in (760, 1024, 1440, 2560):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        page.set_viewport_size({'width': 1440, 'height': 1000})
        panel.screenshot(path=str(options.output_dir / 'review.png'))
        rule.locator('button').first.click()
        page.locator('[data-action="strategy-edit"]').wait_for()
        strategy = StrategyRegistry(store).get('review-' + latest['review_id'])
        assert strategy['status'] == 'draft'
        assert strategy['definition']['rules']['entry'][0]['state'] == 'pending'
        assert strategy['definition']['review_origin']['snapshot'] == latest
        page.locator('.sidebar [data-page="reviews"]').click()
        # A different fill never exposes the selected fill's review or rule draft.
        panel.locator('[data-review-trade]').nth(1).click()
        panel.locator('[role="status"]').wait_for(state='detached')
        assert panel.locator('[aria-label="复盘快照"]').count() == 0
        assert record.locator('[name="uncertainty"]').input_value() == ''
        assert panel.locator('[data-review-form="rule"]').count() == 0
        # Persisted history remains readable after a full frontend restart.
        page.reload()
        page.wait_for_selector('.page-head')
        page.locator('.sidebar [data-page="reviews"]').click()
        panel.locator(f'[data-review-trade="{trade_id}"]').click()
        panel.locator(f'[data-review-id="{original["review_id"]}"]').click()
        result.wait_for()
        assert original['review_id'] in result.inner_text()
        assert registry.get(original['review_id']) == original
        assert not errors, errors
        print('Review browser acceptance passed: real API, null assessments, escaping, lost-response '
              'retry, pending navigation, immutable snapshots, history failure, draft isolation, '
              'pagination, four widths and reload.')


if __name__ == '__main__':
    main()
