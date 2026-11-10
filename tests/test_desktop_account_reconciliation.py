import copy
import csv
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.desktop.account_events import AccountEventLedger
from tradingagents.desktop.account_reconciliation import AccountReconciliation, main
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.trades import COLUMNS, TradeLedger


def event(event_id='e1', **changes):
    return {'broker': 'synthetic', 'account_ref': 'account', 'event_id': event_id,
            'occurred_at': '2024-01-02T10:00:00+08:00', 'source_label': '合成凭证',
            'currency': 'CNY', 'direction': 'deposit', 'amount': '100.00', **changes}


def entry(payload=None):
    return {'kind': 'cash_transfer', 'payload': payload or event()}


def statement(*entries, **changes):
    return {'broker': 'synthetic', 'account_ref': 'account',
            'period_start': '2024-01-02T00:00:00+08:00',
            'period_end': '2024-01-03T00:00:00+08:00', 'source_label': '合成对账单',
            'categories': ['actual_fill', 'cash_transfer', 'cash_dividend', 'split'],
            'entries': list(entries), **changes}


def test_mixed_fills_events_unknown_costs_and_immutable_retry(tmp_path):
    store = DesktopStore(tmp_path)
    ledger = AccountReconciliation(store)
    AccountEventLedger(store).record('cash_transfer', event())
    fill = dict(zip(COLUMNS, ['synthetic', 'account', 'f1', '2024-01-02',
                    '2024-01-02T12:00:00+08:00', '600941', 'CN-A', 'buy', '100',
                    '65.00', 'CNY', ''], strict=True))
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, COLUMNS)
    writer.writeheader()
    writer.writerow(fill)
    trades = TradeLedger(store)
    preview = trades.preview(output.getvalue(), 'synthetic.csv')
    trades.import_csv(output.getvalue(), 'synthetic.csv', preview['sha256'])
    source = statement(entry(), {'kind': 'actual_fill', 'payload': fill})
    result = ledger.record(source, 'r1', 'tester')
    assert result['status'] == 'no_differences_in_declared_scope'
    assert len(result['result']['matched']) == 2
    assert len(result['uncertainties']) == 2
    assert result['performance'] == 'not_evaluated'
    assert result['account_completeness'] == 'not_established'
    assert result['statement'] == source
    AccountEventLedger(store).record('cash_transfer', event('new'))
    assert ledger.record(source, 'r1', 'tester') == result == ledger.get('r1')
    source['entries'][1]['payload']['fees'] = '0'
    assert result['statement']['entries'][1]['payload']['fees'] == ''
    with pytest.raises(ValueError):
        ledger.record(source, 'r1', 'tester')
    fresh = ledger.record(source, 'r2', 'tester')
    assert len(fresh['result']['mismatched']) == 1
    assert len(fresh['result']['missing_from_statement']) == 1
    assert ledger.list(limit=1)['items'] == [fresh]
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM holdings').fetchone()[0] == 0


def test_duplicates_conflicts_missing_and_voided_manifest(tmp_path):
    store = DesktopStore(tmp_path)
    events = AccountEventLedger(store)
    events.record('cash_transfer', event('different', amount='200'))
    events.record('cash_transfer', event('local-only'))
    events.record('cash_transfer', event('conflict'))
    old = events.record('cash_transfer', event('voided'))
    events.void(old['record_id'], 'v1', '合成误录', 'tester')
    source = statement(entry(event('different')), entry(event('external-only')),
                       entry(event('duplicate')), entry(event('duplicate', amount='100')),
                       entry(event('conflict')), entry(event('conflict', amount='300')))
    saved = AccountReconciliation(store).record(source, 'r', 'tester')
    result = saved['result']
    assert saved['status'] == 'differences_found'
    assert len(result['duplicates']) == len(result['conflicts']) == 1
    assert len(result['mismatched']) == len(result['missing_from_statement']) == 1
    assert len(result['missing_from_ledger']) == 2
    assert result['conflicts'][0]['local']['payload']['event_id'] == 'conflict'
    assert saved['excluded_voided'][0]['document']['void']['reason'] == '合成误录'


def test_time_boundaries_timezone_fraction_and_explicit_type_scope(tmp_path):
    store = DesktopStore(tmp_path)
    events = AccountEventLedger(store)
    for name, stamp in [('before', '2024-01-02T01:59:59.999999Z'),
                        ('start', '2024-01-02T02:00:00Z'),
                        ('fraction', '2024-01-02T02:00:00.000001Z'),
                        ('end', '2024-01-02T02:00:01Z')]:
        events.record('cash_transfer', event(name, occurred_at=stamp))
    source = statement(period_start='2024-01-02T10:00:00+08:00',
                       period_end='2024-01-02T02:00:01Z', categories=['cash_transfer'])
    saved = AccountReconciliation(store).record(source, 'r', 'tester')
    assert {row['payload']['event_id'] for row in saved['local_entries']} == {'start', 'fraction'}
    assert not AccountReconciliation(store).record(
        statement(categories=['split']), 'empty', 'tester')['result']['missing_from_statement']


@pytest.mark.parametrize('change', [
    {'categories': []}, {'categories': ['split', 'split']}, {'categories': [{}]},
    {'categories': ['unknown']}, {'period_start': '2024-01-02'},
    {'period_end': '2024-01-01T00:00:00Z'}, {'entries': {}},
    {'entries': [entry(event(account_ref='other'))]},
    {'entries': [entry(event(occurred_at='2024-02-01T00:00:00Z'))]},
    {'categories': ['split'], 'entries': [entry()]},
    {'entries': [{'kind': 'actual_fill', 'payload': {}}]},
    {'entries': [{'kind': [], 'payload': {}}]},
    {'entries': [{'kind': 'split'}]}, {'unexpected': True},
])
def test_invalid_scope_rows_do_not_reserve_request(tmp_path, change):
    ledger = AccountReconciliation(DesktopStore(tmp_path))
    with pytest.raises(ValueError):
        ledger.record(statement(**change), 'r', 'tester')
    assert ledger.list()['total'] == 0
    assert ledger.record(statement(), 'r', 'tester')['status'] == 'no_differences_in_declared_scope'


def test_concurrent_idempotency_and_conflicting_author(tmp_path):
    ledger = AccountReconciliation(DesktopStore(tmp_path))
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: ledger.record(statement(entry()), 'r', 'tester'), range(8)))
    assert all(result == results[0] for result in results)
    assert ledger.list()['total'] == 1
    with pytest.raises(ValueError):
        ledger.record(statement(entry()), 'r', 'someone-else')
    with pytest.raises(ValueError):
        ledger.get('missing')
    for arguments in ({'limit': True}, {'limit': 0}, {'offset': -1}):
        with pytest.raises(ValueError):
            ledger.list(**arguments)


def test_cli_api_workspace_guards_and_persistence(tmp_path, capsys):
    from tradingagents.desktop.api import DesktopAPI
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock

    workspace = tmp_path / 'workspace'
    source = tmp_path / 'statement.json'
    source.write_text(json.dumps(statement(entry()), ensure_ascii=False), encoding='utf-8-sig')
    args = ['--workspace', str(workspace), 'record', str(source),
            '--request-id', 'r', '--recorded-by', 'tester']
    with pytest.raises(SystemExit):
        main(args)
    assert not workspace.exists()
    store = DesktopStore(workspace)
    with WorkspaceLock(workspace), pytest.raises(SystemExit):
        main(args)
    (workspace / INCOMPLETE).touch()
    with pytest.raises(SystemExit):
        main(args)
    (workspace / INCOMPLETE).unlink()
    capsys.readouterr()
    main(args)
    saved = json.loads(capsys.readouterr().out)
    api = DesktopAPI.__new__(DesktopAPI)
    api._store = store
    assert api.get_account_reconciliation('r')['data'] == saved
    assert api.record_account_reconciliation(statement(entry()), 'r', 'tester')['data'] == saved
    assert api.list_account_reconciliations()['data']['total'] == 1
    main(['--workspace', str(workspace), 'get', 'r'])
    assert json.loads(capsys.readouterr().out) == saved
    main(['--workspace', str(workspace), 'list', '--offset', '1'])
    assert json.loads(capsys.readouterr().out) == {'total': 1, 'items': []}
    assert AccountReconciliation(DesktopStore(workspace)).get('r') == copy.deepcopy(saved)


def test_tax_unknown_differs_from_zero_and_kind_conflict_uses_event_identity(tmp_path):
    store = DesktopStore(tmp_path)
    events = AccountEventLedger(store)
    dividend = event()
    del dividend['direction'], dividend['amount']
    dividend.update(ticker='600941', gross_amount='10', tax=None)
    events.record('cash_dividend', dividend)
    ledger = AccountReconciliation(store)
    source = statement({'kind': 'cash_dividend', 'payload': {**dividend, 'tax': '0'}})
    result = ledger.record(source, 'tax', 'tester')
    assert len(result['result']['mismatched']) == 1
    assert result['uncertainties'][0]['field'] == 'tax'
    assert result['uncertainties'][0]['side'] == 'local'
    result = ledger.record(statement(entry()), 'kind', 'tester')
    assert len(result['result']['mismatched']) == 1
    assert not result['result']['missing_from_ledger']


def test_correction_history_backup_and_failed_write_retry(tmp_path):
    from tradingagents.desktop.backup import create_backup, restore_backup

    store = DesktopStore(tmp_path / 'workspace')
    events = AccountEventLedger(store)
    old = events.record('cash_transfer', event())
    events.correct(old['record_id'], 'correct', '合成更正', 'tester',
                   'cash_transfer', event('replacement', amount='200'))
    ledger = AccountReconciliation(store)
    source = statement(entry(event('replacement', amount='200')))
    with store.connect() as db:
        db.execute('''CREATE TRIGGER reject_reconciliation BEFORE INSERT ON account_reconciliations
                      BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record(source, 'r', 'tester')
    assert ledger.list()['total'] == 0
    with store.connect() as db:
        db.execute('DROP TRIGGER reject_reconciliation')
    saved = ledger.record(source, 'r', 'tester')
    assert saved['status'] == 'no_differences_in_declared_scope'
    assert saved['local_entries'][0]['document']['replaces'] == old['record_id']
    assert saved['excluded_voided'][0]['document']['replaced_by']
    archive = tmp_path / 'backup.zip'
    create_backup(store.directory, archive)
    restore_backup(archive, tmp_path / 'restored')
    restored = AccountReconciliation(DesktopStore(tmp_path / 'restored'))
    assert restored.get('r') == saved
    assert restored.record(source, 'r', 'tester') == saved
