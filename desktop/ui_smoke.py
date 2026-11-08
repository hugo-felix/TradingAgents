"""Exercise the actual WebView/JS bridge with temporary local data, no model calls.

Run explicitly: python desktop/ui_smoke.py --native [--show-window] [--report-dir PATH]
"""
from __future__ import annotations

import argparse
import tempfile
import threading
import time
from pathlib import Path

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore

parser = argparse.ArgumentParser()
parser.add_argument('--report-dir', type=Path)
parser.add_argument('--native', action='store_true', help='显式允许启动原生验收进程')
parser.add_argument('--show-window', action='store_true', help='显示验收窗口；默认隐藏')
options = parser.parse_args()
if not options.native:
    parser.error('原生验收必须显式传入 --native；日常验证请使用 browser_smoke.py。')
import webview  # noqa: E402

if __package__:
    from desktop.smoke_runtime import exit_smoke
else:
    from smoke_runtime import exit_smoke

results = {'ok': False}

with tempfile.TemporaryDirectory(prefix='StockSeek smoke ') as directory:
    store = DesktopStore(Path(directory))
    if options.report_dir:
        assert store.import_directory(options.report_dir)['added'] > 0
    else:
        report = Path(directory) / 'fixture'
        report.mkdir()
        (report / 'complete_report.md').write_text(
            '# Trading Analysis Report: AAPL\n- Analysis date: 2026-11-05\n\n'
            '## V. Portfolio Manager Decision\n\n**Rating**: Hold\n\n'
            '**Executive Summary**: Wait for cash flow evidence.\n\n'
            '**Investment Thesis**\n\n1. Cash flow is improving.\n\n2. Valuation is mixed.', encoding='utf-8')
        store.import_directory(report)
    api = DesktopAPI(store)
    window = webview.create_window('StockSeek · UI 验证', Path('tradingagents/desktop/static/index.html').resolve().as_uri(),
                                   js_api=api, width=1320, height=900, text_select=True, hidden=not options.show_window)
    api._window = window

    def finish(result):
        results.update(result)
        window.destroy()

    window.expose(finish)

    script = r'''
(async () => {
const check=(value,message)=>{if(!value)throw Error(message)};
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const wait=async test=>{for(let n=0;n<100;n++){if(test())return;await pause(50)}throw Error('Timed out waiting for UI')};
const click=selector=>{const el=document.querySelector(selector);check(el,'Missing '+selector);el.click()};
try {
 await wait(()=>document.querySelector('[data-page="research"]')&&document.querySelector('.page-head'));
 check(document.querySelector('[data-monitoring-status]'),'Home must state monitoring scope');
 const privateMethods=Object.keys(window.pywebview.api).filter(name=>name.startsWith('_'));
 check(privateMethods.length===0,'Private service exposed');
 check(!window.pywebview.api.store&&!window.pywebview.api.tasks,'Nested services exposed');
 click('.sidebar [data-page="research"]');
 await wait(()=>document.querySelector('[data-report]'));
 click('#content [data-report]');
 await wait(()=>document.querySelector('[role="tablist"]'));
 check(document.querySelector('.conclusion'),'Missing default summary');
 check(document.querySelectorAll('.argument').length<=3,'Too many arguments');
 click('[data-source]');
 await wait(()=>document.querySelector('.source-block.highlight'));
 check(document.querySelector('[data-tab="evidence"]').getAttribute('aria-selected')==='true','Source route failed');
 click('[data-tab="full"]');
 check(document.querySelector('.reader-nav'),'Missing reader');
 click('[data-tab="judgment"]');
 click('[data-action="toggle-decision"]');
 const reason=document.querySelector('#decision-form textarea');
 reason.value='UI smoke: retain cash flow qualification';
 reason.dispatchEvent(new Event('input',{bubbles:true}));
 click('[data-tab="summary"]');click('[data-tab="judgment"]');click('[data-action="toggle-decision"]');
 check(document.querySelector('#decision-form textarea').value.includes('qualification'),'Draft lost');
 document.querySelector('#decision-form').requestSubmit();
 await wait(()=>document.querySelector('.record'));
 check(document.querySelector('.record').textContent.includes('qualification'),'Decision not saved');
 click('.sidebar [data-page="holdings"]');
 click('[data-action="new-holding"]');
 const form=document.querySelector('#holding-form');
 form.elements.ticker.value='AAPL';form.elements.quantity.value='100';form.elements.cost.value='150';form.elements.reason.value='Verified actual snapshot';form.requestSubmit();
 await wait(()=>!document.querySelector('#holding-dialog').open&&document.querySelector('[data-holding="AAPL"]'));
 click('.sidebar [data-page="history"]');
 await wait(()=>document.querySelectorAll('.record').length>=2);
 click('.sidebar [data-page="settings"]');
 check(document.querySelector('#settings-form'),'Settings route');
 click('#theme');await pause(100);
 check(document.documentElement.dataset.theme==='light','Theme failed');
 click('.sidebar [data-page="today"]');
 check(document.documentElement.scrollWidth<=innerWidth,'Horizontal overflow');
 await window.pywebview.api.finish({ok:true,checks:15,privateMethods,viewport:innerWidth});
} catch(error) {await window.pywebview.api.finish({ok:false,error:String(error),body:document.body.textContent.slice(-1500)});}
})();
'''

    def exercise():
        try:
            window.events.loaded.wait(20)
            time.sleep(.5)
            window.run_js(script)
        except Exception as exc:
            results['error'] = repr(exc)
            window.destroy()

    def timeout():
        time.sleep(45)
        if not results.get('ok'):
            results['error'] = results.get('error', 'UI timeout')
        exit_smoke(results, api._tasks.close)

    threading.Thread(target=timeout, daemon=True).start()
    try:
        webview.start(exercise, http_server=False, private_mode=True, storage_path=str(Path(directory) / 'webview'))
    except Exception as exc:
        results['error'] = type(exc).__name__
    finally:
        api._tasks.close()
        api._dividends.close()
exit_smoke(results)
