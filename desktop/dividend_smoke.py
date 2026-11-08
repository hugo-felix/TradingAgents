"""Offline browser check for the dividend snapshot bridge; never fetches market data."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from tradingagents.dataflows.dividend_screen import UNIVERSE, annual_snapshot, atomic_json
from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore


def fixture():
    """Synthetic UI states, independent of personal snapshots and network access."""
    return {
        'schema_version': 1, 'generated_at': datetime.now().astimezone().isoformat(),
        'as_of': datetime.now().date().isoformat(), 'require_qualifications': False,
        'coverage': '3只虚构验收候选', 'rules': '仅用于离线页面验收',
        'limitations': '虚构数据，不代表任何上市公司',
        'items': [{
            'code': f'00000{index}', 'name': f'验收候选{index}', 'sector': '测试行业',
            'status': status, 'metrics': {'yield_basis': 'annual_completed_v2', 'yield_pct': 6 - index, 'yield_ttm_pct': 8 - index,
                                        'dividend_year': 2025, 'price': 10,
                                        'dividend_annual': (6-index)/10, 'dividend_ttm': (8-index)/10, 'market_cap_yi': 500,
                                        'volatility_1y_pct': index*10, 'volatility_3y_pct': index*12,
                                        'max_drawdown_1y_pct': index*5, 'max_drawdown_3y_pct': index*7,
                                        'turnover_20d_yi': index, 'pe_annual': 10, 'quote_time': '测试行情时间',
                                        'risk_as_of': datetime.now().date().isoformat(),
                                        'basic_eps_annual': 1, 'payout_ratio_pct': 50,
                                        'dividend_annual_history': [{'year': datetime.now().year-i, 'dps': .5} for i in range(1,4)]},
            'checks': [{'label': '股息率', 'status': status, 'detail': '测试依据',
                        'required': True},
                       {'label': '现金流补充核验', 'status': 'pending',
                        'detail': '未核验', 'required': False}],
            'sources': {'fixture': 'https://example.org/fixture'}, 'errors': [],
        } for index, status in enumerate(('pass', 'pending', 'fail'), 1)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, help='Optional local snapshot to inspect')
    parser.add_argument('--chromium-path', help='Use an existing Chromium executable')
    options = parser.parse_args()
    output = Path('output/dividend-smoke')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
        api = DesktopAPI(DesktopStore(Path(directory)))
        methods = [name for name in dir(api) if not name.startswith('_') and callable(getattr(api, name))]
        browser = playwright.chromium.launch(executable_path=options.chromium_path)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.expose_function('desktopCall', lambda method, args: getattr(api, method)(*args))
            page.add_init_script('window.pywebview={api:Object.fromEntries(' + json.dumps(methods) +
                                 '.map(name=>[name,(...args)=>window.desktopCall(name,args)]))};')
            page.goto(Path('tradingagents/desktop/static/index.html').resolve().as_uri())
            page.wait_for_selector('.page-head')
            page.locator('[data-page="dividends"]').click()
            page.get_by_text('尚无筛选快照', exact=True).wait_for()
            snapshot = (json.loads(options.snapshot.read_text(encoding='utf-8'))
                        if options.snapshot else fixture())
            if not options.snapshot:
                snapshot['items'][0].update(
                    provenance={'dividends': {'endpoint': 'stock_dividend_cninfo'}},
                    errors=['主分红接口暂不可用', '财务接口缺项'])
            atomic_json(Path(directory) / 'dividend-screen.json', snapshot)
            page.get_by_text('读取最新快照', exact=True).click()
            page.locator('[data-filter="core"][aria-pressed="true"]').wait_for()
            snapshot = annual_snapshot(snapshot)
            assert page.locator('.dividend-overview > button').count() == 3
            assert page.get_by_label('核验范围').is_visible()
            assert '最新季报' in page.get_by_label('核验范围').inner_text()
            assert '0只不代表数据齐全' in page.get_by_label('数据完整性').inner_text()
            core_count = sum(r.get('core', {}).get('status') == 'pass' for r in snapshot['items'])
            if core_count:
                assert f'/{core_count} 只' in page.locator('.dividend-concentration summary').inner_text()
            assert not page.locator('.dividend-records').get_attribute('open')
            assert not page.locator('.dividend-method').get_attribute('open')
            page.locator('.dividend-method > summary').click()
            assert page.get_by_text('年度已实施股息率 =', exact=False).is_visible()
            page.locator('.dividend-method > summary').click()
            assert not page.locator('.dividend-advanced').get_attribute('open')
            checkbox = page.locator('[name="watched"]').bounding_box()
            assert checkbox and checkbox['width'] == 15 and checkbox['height'] == 15
            expected_core = sum(r['core']['status'] == 'pass' for r in snapshot['items'])
            page.wait_for_function('(count)=>document.querySelectorAll(".research-table tbody tr").length===count', arg=expected_core)
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(output / 'dividends-core.png'), full_page=True)
            page.locator('[data-filter="core_pending"]').click()
            assert page.locator('.research-table tbody tr').count() == sum(r['core']['status'] == 'pending' for r in snapshot['items'])
            page.locator('[data-filter="yield"]').click()
            if snapshot['schema_version'] == 1:
                assert page.get_by_role('alert').filter(has_text='尚未完成全市场采集').is_visible()
            expected_yield = [row for row in snapshot['items']
                              if row['metrics'].get('yield_pct') is not None
                              and row['metrics']['yield_pct'] > 4]
            assert page.locator('.research-table tbody tr').count() == len(expected_yield)
            assert page.locator('[data-filter="yield"]').get_attribute('aria-pressed') == 'true'
            # Exactly 4%, below 4%, and missing yields belong only in diagnostic views.
            if not options.snapshot:
                assert page.get_by_text('验收候选1', exact=True).count() == 1
                assert page.get_by_text('TTM 7.00%', exact=True).count() == 1
                page.locator('[data-action="dividend-evidence"]').first.click()
                assert page.get_by_text('年度口径：2025年，每股税前现金分红 0.50元', exact=False).is_visible()
                assert page.get_by_text('分红数据已由备用来源巨潮资讯获取', exact=False).is_visible()
                assert not page.get_by_text('财务接口缺项', exact=True).is_visible()
                page.locator('dialog[open] details summary').click()
                assert page.get_by_text('财务接口缺项', exact=True).is_visible()
                page.keyboard.press('Escape')
                assert page.locator('dialog[open]').count() == 0
                assert page.locator('[data-action="dividend-evidence"]').first.evaluate('(el)=>el===document.activeElement')
                assert page.get_by_text('验收候选2', exact=True).count() == 0
                assert page.get_by_text('验收候选3', exact=True).count() == 0
            page.locator('.dividend-records > summary').click()
            page.locator('[data-filter="all"]').click()
            assert page.locator('.research-table tbody tr').count() == len(snapshot['items'])
            if not options.snapshot:
                evidence = Path(directory) / 'dividend-official-evidence.json'
                atomic_json(evidence, {'schema_version': 1, 'records': [{
                    'code': '000003', 'entity': '虚构上市主体', 'scope': 'listed_entity',
                    'report_date': snapshot['as_of'], 'notice_date': snapshot['as_of'],
                    'source_url': 'https://example.org/disclosure.pdf', 'page': 2,
                    'fields': {'A_LISTING_DATE': snapshot['as_of']}}]})
                page.get_by_text('读取最新快照', exact=True).click()
                page.locator('.dividend-reason').filter(has_text='上市历史不足').wait_for()
                assert page.get_by_text('部分缺项已按官方公告补充', exact=False).is_visible()
                atomic_json(evidence, {'schema_version': 999})
                page.get_by_text('读取最新快照', exact=True).click()
                page.get_by_role('alert').filter(has_text='公告补充证据未应用').wait_for()
                evidence.unlink()
                # Unsorted input, tied values and missing values exercise real table ordering.
                sorting = fixture()
                sorting['items'][0]['metrics'].update(market_cap_yi=800, pe_annual=8)
                sorting['items'][1]['metrics'].update(yield_pct=5, market_cap_yi=900, pe_annual=12)
                sorting['items'][2]['metrics'].update(yield_pct=None, market_cap_yi=None,
                                                     pe_annual=None)
                sorting['items'].append({**sorting['items'][0], 'code': '000004',
                                         'name': '验收候选4',
                                         'metrics': {'yield_basis': 'annual_completed_v2', 'yield_pct': 2, 'market_cap_yi': 700,
                                                     'pe_annual': 6, 'quote_time': '测试'}})
                sorting['items'].reverse()
                atomic_json(Path(directory) / 'dividend-screen.json', sorting)
                page.get_by_text('读取最新快照', exact=True).click()

                def assert_order(indices):
                    expected = [f'验收候选{index}' for index in indices]
                    page.wait_for_function(
                        '(expected)=>JSON.stringify(Array.from(document.querySelectorAll('
                        '".research-table tbody tr td:first-child strong"),el=>el.textContent))'
                        '===JSON.stringify(expected)', arg=expected)
                    assert f'/{len(indices)} 只' in page.locator(
                        '.dividend-concentration summary').inner_text()

                assert_order([1, 2, 4, 3])
                for key, descending in (('yield_pct', [1, 2, 4, 3]),
                                        ('market_cap_yi', [2, 1, 4, 3]),
                                        ('pe_annual', [2, 1, 4, 3])):
                    header = page.locator(f'[data-sort="{key}"]')
                    if key != 'yield_pct':
                        header.click()
                    assert_order(descending)
                    assert header.locator('..').get_attribute('aria-sort') == 'descending'
                    header.focus()
                    page.keyboard.press('Enter')
                    assert_order([4, 1, 2, 3])
                    assert header.locator('..').get_attribute('aria-sort') == 'ascending'
                    header.click()
                    assert_order(descending)
                page.locator('[data-filter="pending"]').click()
                assert_order([2])
                page.locator('[data-filter="all"]').click()
                assert_order([2, 1, 4, 3])
                for key in ('volatility_1y_pct', 'max_drawdown_1y_pct', 'turnover_20d_yi'):
                    page.locator(f'[data-sort="{key}"]').click()
                    assert_order([3, 2, 1, 4])
                    page.locator(f'[data-sort="{key}"]').click()
                    assert_order([1, 2, 3, 4])
                page.locator('.dividend-advanced > summary').click()
                page.locator('[name="maxVol"]').fill('15')
                page.get_by_text('应用筛选', exact=True).click()
                assert_order([1])
                assert page.locator('.dividend-advanced').get_attribute('open') is not None
                page.locator('[name="period"]').locator('..').locator('.select-trigger').click()
                page.get_by_role('option', name='近3年', exact=True).click()
                page.get_by_text('应用筛选', exact=True).click()
                page.locator('[data-sort="volatility_3y_pct"]').click()
                assert_order([1])
                page.get_by_text('重置条件', exact=True).click()
                assert_order([3, 2, 1, 4])
                assert page.locator('[data-filter="all"]').get_attribute('aria-pressed') == 'true'
                page.locator('[data-filter="all"]').click()
                page.locator('.dividend-advanced > summary').click()
                page.locator('[name="minTurnover"]').fill('2')
                page.get_by_text('应用筛选', exact=True).click()
                assert_order([3, 2])
                page.get_by_text('重置条件', exact=True).click()
                page.locator('[data-filter="all"]').click()
                atomic_json(Path(directory) / 'dividend-screen.json', snapshot)
                page.get_by_text('读取最新快照', exact=True).click()
                page.locator('[data-sort="yield_pct"]').click()
                assert_order([1, 2, 3])
            for status in ('pass', 'pending', 'fail', 'all'):
                page.locator(f'.dividend-stat[data-filter="{status}"]').click()
                expected = len(snapshot['items']) if status == 'all' else sum(
                    row['status'] == status for row in snapshot['items'])
                assert page.locator('.research-table tbody tr').count() == expected
                assert page.locator(f'.dividend-stat[data-filter="{status}"]').get_attribute('aria-pressed') == 'true'
            assert page.locator('.dividend-reason').first.inner_text()
            page.locator('[data-action="dividend-evidence"]').first.click()
            first_check = snapshot['items'][0]['checks'][0]['label']
            page.get_by_text(first_check, exact=False).first.wait_for()
            page.screenshot(path=str(output / 'dividends-evidence.png'), full_page=True)
            page.get_by_role('button', name='关闭核验依据').click()
            page.screenshot(path=str(output / 'dividends.png'), full_page=True)
            page.set_viewport_size({'width': 1024, 'height': 700})
            assert page.locator('body').evaluate('(el)=>el.scrollWidth<=innerWidth')
            page.set_viewport_size({'width': 760, 'height': 600})
            assert page.locator('body').evaluate('(el)=>el.scrollWidth<=innerWidth')
            page.screenshot(path=str(output / 'dividends-narrow.png'), full_page=True)
            page.evaluate('document.documentElement.dataset.theme="light"')
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.screenshot(path=str(output / 'dividends-light.png'), full_page=True,
                            animations='disabled')
            # Follow, annotate and filter through the actual persisted API bridge.
            code = snapshot['items'][0]['code']
            page.locator(f'[data-action="dividend-watch"][data-code="{code}"]').click()
            note = page.locator(f'[data-watch-code="{code}"] textarea')
            note.fill('等待合适价格；下次复核分红')
            page.get_by_role('button', name='保存备注', exact=True).click()
            page.get_by_text('自选备注已保存。', exact=True).wait_for()
            page.get_by_text('读取最新快照', exact=True).click()
            page.locator('.dividend-watches summary').click()
            assert note.input_value() == '等待合适价格；下次复核分红'
            page.locator('[name="watched"]').check()
            page.get_by_text('应用筛选', exact=True).click()
            assert page.locator('.research-table tbody tr').count() == 1
            page.get_by_text('重置条件', exact=True).click()
            page.locator('[data-filter="all"]').click()
            snapshot['changes'] = {
                'previous_at': snapshot['generated_at'],
                'items': [{'code': code, 'name': snapshot['items'][0]['name'],
                           'kind': 'updated', 'reasons': ['离线验收：年度股息率跨过4%门槛']}],
            }
            atomic_json(Path(directory) / 'dividend-screen.json', snapshot)
            page.get_by_text('读取最新快照', exact=True).click()
            page.locator('.dividend-changes summary').click()
            page.get_by_text('离线验收：年度股息率跨过4%门槛', exact=True).wait_for()
            assert page.locator('.dividend-freshness time').get_attribute('datetime')
            # A corrupt replacement must show an error while retaining the last good snapshot.
            atomic_json(Path(directory) / 'dividend-screen.json',
                        {**snapshot, 'items': [{'metrics': None}]})
            page.get_by_text('读取最新快照', exact=True).click()
            page.get_by_text('高股息快照格式无效，请点击“刷新筛选”重新获取。', exact=True).wait_for()
            assert page.locator('.research-table tbody tr').count() == len(snapshot['items'])
            # Exercise the real background manager with a deterministic offline worker.
            refreshed = fixture()
            refreshed['items'] = [{**refreshed['items'][1], 'code': code,
                                   'name': f'离线验收 {code}'} for code, _, _ in UNIVERSE]
            atomic_json(Path(directory) / 'refresh-fixture.json', refreshed)
            worker = Path(directory) / 'refresh_worker.py'
            worker.write_text(
                'import sys,time\nfrom pathlib import Path\n'
                'folder=Path(sys.argv[sys.argv.index("--data-dir")+1])\n'
                'root=folder.parent.parent\n'
                'print("600941 fixture: pending",flush=True)\n'
                'time.sleep(3)\n'
                '(folder/"dividend-screen.json").write_bytes('
                '(root/"refresh-fixture.json").read_bytes())\n', encoding='utf-8')
            api._dividends.command_builder = lambda: [sys.executable, str(worker)]
            page.get_by_text('刷新筛选', exact=True).click()
            page.get_by_text('取消刷新', exact=True).wait_for()
            page.locator('[data-page="research"]').click()
            page.locator('[data-page="dividends"]').click()
            page.get_by_text('离线验收 600941', exact=True).wait_for(timeout=20000)
            assert page.locator('.research-table tbody tr').count() == len(UNIVERSE)
            page.locator('[data-filter="pass"]').click()
            page.get_by_text('当前分类没有候选', exact=True).wait_for()
            page.locator('[data-filter="all"]').click()
            assert page.locator('.research-table tbody tr').count() == len(UNIVERSE)
            published = (Path(directory) / 'dividend-screen.json').read_bytes()
            page.get_by_text('刷新筛选', exact=True).click()
            page.get_by_text('取消刷新', exact=True).click()
            page.get_by_text('刷新已取消，旧清单已保留', exact=True).wait_for(timeout=20000)
            assert (Path(directory) / 'dividend-screen.json').read_bytes() == published
            worker.write_text('raise SystemExit(2)', encoding='utf-8')
            page.get_by_text('刷新筛选', exact=True).click()
            page.get_by_text('重试刷新', exact=True).wait_for(timeout=20000)
            assert (Path(directory) / 'dividend-screen.json').read_bytes() == published
            page.locator('.dividend-watches summary').click()
            page.get_by_text('未出现在当前清单；不能据此判断股息或财务恶化。', exact=True).wait_for()
            assert note.input_value() == '等待合适价格；下次复核分红'
            page.locator(f'[data-action="dividend-unwatch"][data-code="{code}"]').click()
            assert api.dividend_watchlist()['data'] == []
            assert not errors, errors
            print('Dividend sorting, bridge, layout, refresh, navigation, cancel and failure passed.')
        finally:
            api._tasks.close()
            api._dividends.close()
            browser.close()


if __name__ == '__main__':
    main()
