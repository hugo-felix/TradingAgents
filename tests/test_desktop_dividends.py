import json
import sys
import threading
from datetime import datetime

import pytest

from tradingagents.dataflows.dividend_screen import UNIVERSE, atomic_json, screen_command
from tradingagents.desktop.dividends import DividendJobs
from tradingagents.desktop.store import DesktopStore


def test_auto_updates_are_bounded_persistent_and_optional(tmp_path, monkeypatch):
    manager = DividendJobs(DesktopStore(tmp_path))
    atomic_json(tmp_path / 'dividend-screen.json', {
        'generated_at': '2026-11-05T18:05:00+08:00', 'items': [],
        'discovery': {'roster': {'status': 'confirmed', 'missing_quote_codes': []}}})
    launches = []
    monkeypatch.setattr(manager, 'start', lambda **kwargs: launches.append(kwargs))
    try:
        for hour in (18, 18, 19, 20, 21):
            manager.auto_tick(datetime.fromisoformat(f'2026-11-06T{hour}:01:00+08:00'))
        assert len(launches) == 3
        manager.auto_tick(datetime.fromisoformat('2026-11-07T10:01:00+08:00'))
        assert len(launches) == 4
        manager.set_auto_update(False)
        manager.auto_tick(datetime.fromisoformat('2026-11-08T10:01:00+08:00'))
        assert len(launches) == 4
        assert manager.status()['auto_update'] is False
        with pytest.raises(ValueError):
            manager.set_auto_update('false')
    finally:
        manager.close()


def test_auto_update_keeps_fresh_snapshot_and_respects_cancellation(tmp_path, monkeypatch):
    manager = DividendJobs(DesktopStore(tmp_path))
    payload = {'generated_at': '2026-11-06T18:05:00+08:00', 'items': [],
               'as_of': '2026-11-07',
               'market_context': {'as_of': '2026-11-07', 'market': {'status': 'normal'}},
               'discovery': {'roster': {'status': 'confirmed', 'missing_quote_codes': []}}}
    atomic_json(tmp_path / 'dividend-screen.json', payload)
    launches = []
    monkeypatch.setattr(manager, 'start', lambda **kwargs: launches.append(kwargs))
    try:
        manager.auto_tick(datetime.fromisoformat('2026-11-07T10:01:00+08:00'))
        assert not launches
        payload['discovery']['roster']['missing_quote_codes'] = ['601318']
        atomic_json(tmp_path / 'dividend-screen.json', payload)
        manager.current = {'status': 'running'}
        manager.auto_tick(datetime.fromisoformat('2026-11-07T11:01:00+08:00'))
        assert not launches
        manager.current = {'status': 'cancelled', 'finished_at': '2026-11-07T10:00:00+08:00'}
        manager.auto_tick(datetime.fromisoformat('2026-11-07T11:01:00+08:00'))
        assert not launches
        manager.current = {'status': 'idle'}
        manager.auto_tick(datetime.fromisoformat('2026-11-07T12:01:00+08:00'))
        assert len(launches) == 1
    finally:
        manager.close()


def test_auto_update_holiday_uses_last_session_and_keeps_catchup(tmp_path, monkeypatch):
    from datetime import date, timedelta

    dates = [(date(2025, 1, 1) + timedelta(days=n)).isoformat() for n in range(650)
             if (date(2025, 1, 1) + timedelta(days=n)).weekday() < 5
             and not '2026-10-01' <= (date(2025, 1, 1) + timedelta(days=n)).isoformat()
             <= '2026-10-07']
    payload = {'generated_at': '2026-09-30T18:05:00+08:00', 'items': [],
               'as_of': '2026-09-30',
               'market_context': {'as_of': '2026-09-30', 'market': {'status': 'normal'}},
               'discovery': {'roster': {'status': 'confirmed'}},
               'trading_calendar': {'status': 'available', 'dates': dates,
                                    'first_date': dates[0], 'last_date': dates[-1]}}
    manager = DividendJobs(DesktopStore(tmp_path))
    launches = []
    monkeypatch.setattr(manager, 'start', lambda **kwargs: launches.append(kwargs))
    try:
        atomic_json(tmp_path / 'dividend-screen.json', payload)
        manager.auto_tick(datetime.fromisoformat('2026-10-06T19:00:00+08:00'))
        assert not launches
        payload['generated_at'] = '2026-09-29T18:05:00+08:00'
        atomic_json(tmp_path / 'dividend-screen.json', payload)
        manager.auto_tick(datetime.fromisoformat('2026-10-06T20:00:00+08:00'))
        assert len(launches) == 1  # Still catch up an old snapshot during holidays.
    finally:
        manager.close()


def snapshot():
    return {
        'schema_version': 1, 'generated_at': datetime.now().astimezone().isoformat(),
        'as_of': '2026-11-07', 'coverage': 'offline fixtures', 'rules': 'fixture',
        'limitations': 'fixture', 'require_qualifications': True,
        'items': [{'code': code, 'name': name, 'sector': sector, 'status': 'pending',
                   'metrics': {}, 'checks': [{'status': 'pending', 'required': True,
                                             'label': 'fixture', 'detail': 'fixture'}]}
                  for code, name, sector in UNIVERSE],
    }


def jobs(tmp_path, payload=None, tail=''):
    store = DesktopStore(tmp_path / 'workspace')
    script = tmp_path / 'worker.py'
    fixture = tmp_path / 'fixture.json'
    atomic_json(fixture, snapshot() if payload is None else payload)
    script.write_text(
        'import sys, json, time\nfrom pathlib import Path\n'
        'folder=Path(sys.argv[sys.argv.index("--data-dir")+1])\n'
        f'data=Path({str(fixture)!r}).read_text(encoding="utf-8")\n'
        '(folder/"dividend-screen.json").write_text(data, encoding="utf-8")\n'
        '(folder/"args.json").write_text(json.dumps(sys.argv), encoding="utf-8")\n'
        'print("600941 fixture: pending", flush=True)\n'
        + tail, encoding='utf-8')
    manager = DividendJobs(store, command_builder=lambda: [sys.executable, str(script)])
    return manager


def wait(manager):
    manager.thread.join(timeout=10)
    assert not manager.thread.is_alive()
    return manager.status()


def test_publish_complete_snapshot_ignores_obsolete_strict_policy(tmp_path):
    manager = jobs(tmp_path)
    atomic_json(manager.store.directory / 'dividend-screen.json', snapshot())
    atomic_json(manager.store.directory / 'dividend-evidence.json', {'fixture': True})
    try:
        started = manager.start(direct=True)
        assert wait(manager)['status'] == 'completed'
        assert manager.status()['completed'] == len(UNIVERSE)
        assert 'offline fixtures' in (manager.store.directory / 'dividend-screen.md').read_text(
            encoding='utf-8')
        folder = manager.store.directory / 'dividend-runs' / started['id']
        args = json.loads((folder / 'args.json').read_text(encoding='utf-8'))
        assert '--direct' in args and '--require-qualifications' not in args
        assert not (folder / 'dividend-evidence.json').exists()
        assert json.loads((manager.store.directory / 'dividend-evidence.json').read_text()) == {'fixture': True}
        assert json.loads((manager.store.directory / 'dividend-screen.json').read_text(
            encoding='utf-8'))['coverage'] == 'offline fixtures'
    finally:
        manager.close()


@pytest.mark.parametrize('mode', ['exit', 'malformed', 'incomplete', 'duplicate', 'unavailable'])
def test_failed_refresh_preserves_original_bytes(tmp_path, mode):
    data = snapshot()
    if mode == 'malformed':
        data['items'] = [{}]
    elif mode == 'incomplete':
        data['items'] = data['items'][:1]
    elif mode == 'duplicate':
        data['items'].append(data['items'][0])
    elif mode == 'unavailable':
        for row in data['items']:
            row['errors'] = ['provider unavailable']
    manager = jobs(tmp_path, data, 'sys.exit(2)' if mode == 'exit' else '')
    path = manager.store.directory / 'dividend-screen.json'
    path.write_bytes(b'original')
    try:
        manager.start()
        assert wait(manager)['status'] == 'failed'
        assert path.read_bytes() == b'original'
    finally:
        manager.close()


@pytest.mark.parametrize('status', ['pass', 'pending', 'fail'])
def test_fallback_success_publishes_despite_primary_errors(tmp_path, status):
    from tradingagents.dataflows.dividend_screen import DIVIDENDS_FALLBACK, fetch_dividends
    from tradingagents.dataflows.errors import VendorUnavailableError

    class FallbackSource:
        def fetch(self, endpoint, symbol):
            if endpoint != DIVIDENDS_FALLBACK:
                raise VendorUnavailableError('primary unavailable')
            return {'endpoint': endpoint, 'symbol': symbol, 'rows': []}

    data = snapshot()
    for row in data['items']:
        _, provenance, errors = fetch_dividends(FallbackSource(), row['code'])
        row.update(status=status, errors=errors, provenance={'dividends': provenance})
        row['checks'][0]['status'] = status
    manager = jobs(tmp_path, data)
    path = manager.store.directory / 'dividend-screen.json'
    path.write_bytes(b'previous')
    try:
        manager.start()
        assert wait(manager)['status'] == 'completed'
        published = json.loads(path.read_text(encoding='utf-8'))
        assert published.pop('changes') == {'previous_at': None, 'items': []}
        assert published == data
    finally:
        manager.close()


def test_cancel_does_not_publish_even_if_worker_wrote_snapshot(tmp_path):
    manager = jobs(tmp_path, tail='time.sleep(60)')
    path = manager.store.directory / 'dividend-screen.json'
    path.write_bytes(b'original')
    try:
        started = manager.start()
        folder = manager.store.directory / 'dividend-runs' / started['id']
        for _ in range(100):
            if (folder / 'args.json').exists():
                break
            threading.Event().wait(.02)
        assert (folder / 'args.json').exists()
        with pytest.raises(ValueError, match='已有筛选'):
            manager.start()
        manager.cancel()
        assert wait(manager)['status'] == 'cancelled'
        assert path.read_bytes() == b'original'
        assert manager.process.poll() is not None
    finally:
        manager.close()
    with pytest.raises(ValueError, match='关闭'):
        manager.start()


def test_restart_recovers_interrupted_state(tmp_path):
    store = DesktopStore(tmp_path)
    store.setting('dividend_job', {'status': 'running', 'completed': 2})
    manager = DividendJobs(store)
    assert manager.status()['status'] == 'interrupted'
    assert store.setting('dividend_job')['completed'] == 2
    with pytest.raises(ValueError, match='直连'):
        manager.start('false')


def test_frozen_worker_command_routes_without_launching_window(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert screen_command()[1:] == ['--dividend-worker']


def test_app_routes_frozen_dividend_worker(monkeypatch):
    from tradingagents.dataflows import dividend_screen
    from tradingagents.desktop.app import main
    monkeypatch.setattr(sys, 'argv', ['StockSeekWorker', '--dividend-worker', '--worker',
                                      'stock_fhps_detail_em', '600941'])
    monkeypatch.setattr(dividend_screen, 'main', lambda: tuple(sys.argv))
    assert main() == ('StockSeekWorker', '--worker', 'stock_fhps_detail_em', '600941')


def test_dynamic_market_snapshot_publishes_without_legacy_pool(tmp_path):
    data = snapshot()
    data.update(schema_version=2, require_qualifications=False)
    data['items'] = [{**data['items'][0], 'code': '688999'}]
    data['discovery'] = {'scope': 'all_a', 'scanned': 5000, 'excluded_small_cap': 4999,
                             'unknown_cap': 0, 'evaluated': 1, 'risk_complete': 0,
                             'provider_error_count': 0, 'candidate_codes': ['688999']}
    manager = jobs(tmp_path, data)
    try:
        manager.start()
        state = wait(manager)
        assert state['status'] == 'completed'
        assert state['total'] == state['completed'] == 1
    finally:
        manager.close()


@pytest.mark.parametrize('fault', ['missing', 'count', 'duplicate', 'partial'])
def test_invalid_market_manifest_preserves_previous_snapshot(tmp_path, fault):
    data = snapshot()
    data.update(schema_version=2, require_qualifications=False)
    codes = [r['code'] for r in data['items']]
    data['discovery'] = {'scope': 'all_a', 'scanned': 5000, 'excluded_small_cap': 5000-len(codes),
                             'unknown_cap': 0, 'evaluated': len(codes), 'risk_complete': 0,
                             'provider_error_count': 0, 'candidate_codes': codes}
    if fault == 'missing':
        data['items'].pop()
    elif fault == 'count':
        data['discovery']['evaluated'] = 0
    elif fault == 'duplicate':
        data['items'][-1] = data['items'][0]
    else:
        data['discovery']['scanned'] = 100
    manager = jobs(tmp_path, data)
    target = manager.store.directory / 'dividend-screen.json'
    target.write_bytes(b'previous')
    try:
        manager.start()
        assert wait(manager)['status'] == 'failed'
        assert target.read_bytes() == b'previous'
    finally:
        manager.close()


@pytest.mark.parametrize('fault', [None, 'balance', 'omitted', 'duplicate', 'status'])
def test_exchange_reconciliation_requires_balanced_and_retained_codes(fault):
    from tradingagents.desktop.api import validate_dividend_snapshot

    data = snapshot()
    codes = [row['code'] for row in data['items']]
    data.update(schema_version=2, require_qualifications=False)
    data['discovery'] = {
        'scope': 'all_a', 'scanned': 5000, 'excluded_small_cap': 5000 - len(codes),
        'unknown_cap': 0, 'evaluated': len(codes), 'risk_complete': 0,
        'provider_error_count': 0, 'candidate_codes': codes,
        'roster': {'status': 'confirmed', 'expected': 5000, 'observed': 4999,
                   'missing_quote_codes': [codes[0]], 'unlisted_quote_codes': []},
    }
    roster = data['discovery']['roster']
    if fault == 'balance':
        roster['expected'] += 1
    elif fault == 'omitted':
        roster['missing_quote_codes'] = ['999999']
    elif fault == 'duplicate':
        roster['missing_quote_codes'] *= 2
    elif fault == 'status':
        roster['status'] = 'complete'
    if fault:
        with pytest.raises(ValueError):
            validate_dividend_snapshot(data)
    else:
        validate_dividend_snapshot(data)


def comparison_fixture():
    data = snapshot()
    data['core_rules'] = 'unchanged rules'
    data['items'] = data['items'][:2]
    for row in data['items']:
        row['core'] = {'status': 'pass', 'checks': []}
        row['metrics'] = {'yield_basis': 'annual_completed_v2', 'yield_pct': 5,
                          'dividend_annual': .5, 'dividend_year': 2025}
    return data


def test_changes_explain_exit_threshold_and_missing_scope():
    import copy

    from tradingagents.desktop.dividends import snapshot_changes
    old = comparison_fixture()
    new = copy.deepcopy(old)
    new['items'].pop()
    row = new['items'][0]
    row['core'] = {'status': 'pending', 'checks': [
        {'status': 'pending', 'label': '财务核验', 'required': True}]}
    row['metrics'].update(yield_pct=4, dividend_annual=.4)
    changes = snapshot_changes(old, new)
    by_code = {item['code']: item for item in changes['items']}
    changed = by_code[row['code']]
    assert changed['kind'] == 'exited'
    assert any('待核验' in reason for reason in changed['reasons'])
    assert any('不再超过4%' in reason for reason in changed['reasons'])
    assert any('同年度' in reason for reason in changed['reasons'])
    assert '不能据此判断' in by_code[old['items'][1]['code']]['reasons'][0]


def test_changes_only_compare_compatible_known_observations():
    import copy

    from tradingagents.desktop.dividends import snapshot_changes
    old = comparison_fixture()
    new = copy.deepcopy(old)
    new['items'][0]['metrics'].update(yield_pct=None, dividend_year=2026, dividend_annual=.1)
    assert snapshot_changes(old, new)['items'] == []
    new['items'][0]['metrics'].update(yield_pct=2, yield_basis='ttm')
    assert snapshot_changes(old, new)['items'] == []
    new['core_rules'] = 'changed rules'
    assert snapshot_changes(old, new)['previous_at'] is None
    new['core_rules'] = old['core_rules']
    new['discovery'] = {'scope': 'subset'}
    assert snapshot_changes(old, new)['previous_at'] is None
    assert snapshot_changes(None, new)['previous_at'] is None
    assert snapshot_changes(snapshot(), new)['previous_at'] is None


def test_success_publishes_changes_with_snapshot(tmp_path):
    import copy
    old = comparison_fixture()
    new = copy.deepcopy(old)
    # Legacy fixed-universe workers must still publish the complete universe.
    old['items'][0]['core']['status'] = 'pending'
    manager = jobs(tmp_path, new)
    # Use the full fixed universe, retaining comparable core metadata.
    old['items'] += snapshot()['items'][2:]
    new['items'] += snapshot()['items'][2:]
    for data in (old, new):
        for row in data['items'][2:]:
            row['core'] = {'status': 'pending', 'checks': []}
    atomic_json(tmp_path / 'fixture.json', new)
    atomic_json(manager.store.directory / 'dividend-screen.json', old)
    try:
        manager.start()
        assert wait(manager)['status'] == 'completed'
        published = json.loads((manager.store.directory / 'dividend-screen.json').read_text(
            encoding='utf-8'))
        assert published['changes']['previous_at'] == old['generated_at']
        assert published['changes']['items'][0]['kind'] == 'entered'
    finally:
        manager.close()


def test_watchlist_persists_notes_without_mutating_holdings(tmp_path):
    from tradingagents.desktop.api import DesktopAPI
    store = DesktopStore(tmp_path)
    api = DesktopAPI(store)
    try:
        assert api.save_dividend_watch('600941', '中国移动', '等价格')['ok']
        result = api.save_dividend_watch('600941', '中国移动')['data']
        assert len(result) == 1 and result[0]['note'] == '等价格'
        assert DesktopStore(tmp_path).setting('dividend_watchlist') == result
        assert store.holdings() == []
        assert api.save_dividend_watch('600941', '中国移动', '')['data'][0]['note'] == ''
        assert not api.save_dividend_watch('６００９４１', '无效')['ok']
        assert not api.save_dividend_watch('600941', '', 'x')['ok']
        assert not api.save_dividend_watch('600941', '中国移动', 'x'*2001)['ok']
        assert api.remove_dividend_watch('600941')['data'] == []
    finally:
        api._tasks.close()
        api._dividends.close()


@pytest.mark.parametrize('changes', [[], {'items': [{}]},
    {'previous_at': '2026-01-01', 'items': []},
    {'items': [{'code': '600941', 'name': 'fixture', 'kind': 'exited', 'reasons': None}]}])
def test_malformed_changes_rejected(changes):
    from tradingagents.desktop.api import validate_dividend_snapshot
    data = snapshot()
    data['changes'] = changes
    with pytest.raises(ValueError):
        validate_dividend_snapshot(data)


def test_fresh_legacy_snapshot_backfills_only_environment_with_bounded_retries(tmp_path, monkeypatch):
    manager = DividendJobs(DesktopStore(tmp_path))
    payload = {'generated_at': '2026-11-06T18:05:00+08:00', 'as_of': '2026-11-07',
               'items': [], 'discovery': {'roster': {'status': 'confirmed'}}}
    atomic_json(tmp_path / 'dividend-screen.json', payload)
    launches = []
    monkeypatch.setattr(manager, 'start', lambda **kwargs: launches.append(kwargs))
    try:
        for hour in (10, 10, 11, 12, 13):
            manager.auto_tick(datetime.fromisoformat(f'2026-11-07T{hour}:01:00+08:00'))
        assert len(launches) == 3
        assert all(call['environment_only'] for call in launches)
    finally:
        manager.close()


def test_environment_refresh_preserves_screen_history_and_dates(tmp_path):
    payload = snapshot()
    payload['changes'] = {'previous_at': '2026-11-06T18:00:00+08:00', 'items': [
        {'code': '600941', 'name': '中国移动', 'kind': 'entered', 'reasons': ['通过筛选']}]}
    manager = jobs(tmp_path, payload)
    atomic_json(manager.store.directory / 'dividend-screen.json', payload)
    try:
        started = manager.start(environment_only=True)
        assert wait(manager)['status'] == 'completed'
        folder = manager.store.directory / 'dividend-runs' / started['id']
        args = json.loads((folder / 'args.json').read_text(encoding='utf-8'))
        assert '--environment-snapshot' in args
        result = json.loads((manager.store.directory / 'dividend-screen.json').read_text(
            encoding='utf-8'))
        assert result['changes'] == payload['changes']
        assert result['generated_at'] == payload['generated_at']
        assert result['items'] == payload['items']
    finally:
        manager.close()


def test_recent_generation_with_old_analysis_date_requires_full_refresh(tmp_path, monkeypatch):
    manager = DividendJobs(DesktopStore(tmp_path))
    atomic_json(tmp_path / 'dividend-screen.json', {
        'generated_at': '2026-11-06T18:05:00+08:00', 'as_of': '2026-10-01', 'items': [],
        'discovery': {'roster': {'status': 'confirmed'}}})
    launches = []
    monkeypatch.setattr(manager, 'start', lambda **kwargs: launches.append(kwargs))
    try:
        manager.auto_tick(datetime.fromisoformat('2026-11-07T10:01:00+08:00'))
        assert len(launches) == 1
        assert launches[0]['environment_only'] is False
    finally:
        manager.close()
