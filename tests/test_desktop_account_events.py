import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.desktop import account_events
from tradingagents.desktop.account_events import AccountEventLedger
from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.trades import TradeLedger

from .test_desktop_trades import document, fill, ingest


def payload(kind='cash_transfer', **changes):
    result = {'broker': 'synthetic-broker', 'account_ref': 'synthetic-account',
              'event_id': 'event-1', 'occurred_at': '2024-01-02T10:15:00+08:00',
              'source_label': '合成账户对账单'}
    if kind == 'cash_transfer':
        result.update(currency='CNY', direction='deposit', amount='0100.00')
    elif kind == 'cash_dividend':
        result.update(ticker='600941', currency='CNY', gross_amount='10.50', tax=None)
    else:
        result.update(ticker='600941', ratio_before='1', ratio_after='2')
    return {**result, **changes}


def test_events_preserve_raw_inputs_and_do_not_apply_to_holdings_or_fills(tmp_path):
    store = DesktopStore(tmp_path)
    with store.connect() as db:
        db.execute('INSERT INTO holdings VALUES (?, ?, ?)', ('600941.SS', 7, 12.3))
    trades = TradeLedger(store)
    ingest(trades, document(fill()))
    original_fills = trades.list_fills()
    ledger = AccountEventLedger(store)
    submitted = payload()
    transfer = ledger.record('cash_transfer', submitted)
    dividend = ledger.record('cash_dividend', payload('cash_dividend', event_id='dividend'))
    split = ledger.record('split', payload('split', event_id='split'))
    assert transfer['raw'] == submitted
    assert transfer['payload']['amount'] == '100'
    assert transfer['payload']['occurred_at'] == '2024-01-02T02:15:00+00:00'
    assert dividend['payload']['tax'] is None
    assert split['payload']['ticker'] == '600941.SS'
    assert split['verification'] == 'caller_declared_unverified'
    assert split['effects'] == 'not_applied'
    assert split['account_completeness'] == 'not_established'
    assert split['performance'] == 'not_evaluated'
    submitted['amount'] = '999'
    transfer['payload']['amount'] = '999'
    assert ledger.get(transfer['record_id'])['payload']['amount'] == '100'
    assert ledger.get(transfer['record_id'])['raw']['amount'] == '0100.00'
    assert trades.list_fills() == original_fills
    with store.connect() as db:
        assert tuple(db.execute('SELECT * FROM holdings').fetchone()) == ('600941.SS', 7, 12.3)
        assert db.execute('SELECT COUNT(*) FROM reports').fetchone()[0] == 0


def test_event_identity_is_account_scoped_cross_kind_and_independent_of_date(tmp_path):
    ledger = AccountEventLedger(DesktopStore(tmp_path))
    original = ledger.record('cash_transfer', payload())
    repeated = ledger.record('cash_transfer', payload(amount='100',
                             occurred_at='2024-01-02T02:15:00Z'))
    assert repeated == {**original, 'already_recorded': True}
    for changes in ({'amount': '101'}, {'direction': 'withdrawal'},
                    {'occurred_at': '2024-01-03T10:15:00+08:00'}, {'source_label': 'other'}):
        with pytest.raises(ValueError, match='不能覆盖'):
            ledger.record('cash_transfer', payload(**changes))
    with pytest.raises(ValueError, match='不能覆盖'):
        ledger.record('cash_dividend', payload('cash_dividend'))
    assert ledger.list()['total'] == 1
    ledger.record('cash_transfer', payload(account_ref='other-account'))
    ledger.record('cash_transfer', payload(broker='other-broker'))
    assert ledger.list()['total'] == 3
    assert ledger.get(original['record_id'])['raw'] == original['raw']


def test_zero_tax_is_distinct_from_unknown_and_splits_preserve_ratio(tmp_path):
    ledger = AccountEventLedger(DesktopStore(tmp_path))
    saved = ledger.record('cash_dividend', payload('cash_dividend', tax='0.00'))
    assert saved['payload']['tax'] == '0'
    with pytest.raises(ValueError):
        ledger.record('cash_dividend', payload('cash_dividend', tax=None))
    reverse_split = ledger.record('split', payload('split', event_id='reverse',
                                  ratio_before='10.00', ratio_after='1.0'))
    assert reverse_split['payload']['ratio_before'] == '10'
    assert reverse_split['payload']['ratio_after'] == '1'


@pytest.mark.parametrize(('kind', 'changes'), [
    ('unknown', {}), ('cash_transfer', {'amount': 100}), ('cash_transfer', {'amount': '0'}),
    ('cash_transfer', {'amount': '-1'}), ('cash_transfer', {'amount': 'NaN'}),
    ('cash_transfer', {'direction': 'buy'}), ('cash_transfer', {'currency': 'cny'}),
    ('cash_transfer', {'occurred_at': '2024-01-01T00:00:00'}),
    ('cash_transfer', {'occurred_at': 'invalid'}), ('cash_transfer', {'broker': ''}),
    ('cash_transfer', {'event_id': True}), ('cash_transfer', {'source_label': 'bad\nsource'}),
    ('cash_transfer', {'extra': 'not allowed'}), ('cash_dividend', {'tax': '-1'}),
    ('cash_dividend', {'tax': 0}), ('cash_dividend', {'gross_amount': '0'}),
    ('cash_dividend', {'ticker': ''}), ('split', {'ratio_before': '0'}),
    ('split', {'ratio_after': '-1'}), ('split', {'ratio_after': None}),
])
def test_invalid_events_never_write(tmp_path, kind, changes):
    ledger = AccountEventLedger(DesktopStore(tmp_path))
    with pytest.raises(ValueError):
        ledger.record(kind, payload(kind, **changes))
    assert ledger.list()['total'] == 0


def test_concurrent_event_conflicts_and_retries_are_atomic(tmp_path):
    ledger = AccountEventLedger(DesktopStore(tmp_path))

    def record(amount):
        try:
            return ledger.record('cash_transfer', payload(amount=amount))
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(record, ['100', '200']))
    assert sum(row is not None for row in results) == 1
    assert ledger.list()['total'] == 1
    saved = ledger.list()['items'][0]
    with ThreadPoolExecutor(2) as pool:
        retries = list(pool.map(lambda _: ledger.record('cash_transfer', saved['payload']), range(2)))
    assert all(row['already_recorded'] for row in retries)
    assert all(row['raw'] == saved['raw'] for row in retries)


def test_filters_append_order_pagination_and_incomplete_performance_status(tmp_path):
    ledger = AccountEventLedger(DesktopStore(tmp_path))
    first = ledger.record('cash_transfer', payload())
    ledger.record('cash_dividend', payload('cash_dividend', event_id='dividend'))
    last = ledger.record('cash_transfer', payload(event_id='withdrawal', direction='withdrawal'))
    assert ledger.list(kind='cash_transfer', limit=1)['items'][0]['record_id'] == last['record_id']
    assert ledger.list(kind='cash_transfer', limit=1, offset=1)['items'][0]['record_id'] == first[
        'record_id']
    assert ledger.list('synthetic-broker', 'synthetic-account')['total'] == 3
    assert ledger.list(account_ref='unknown')['items'] == []
    assert ledger.list(offset=3)['total'] == 3
    assert ledger.list(offset=3)['items'] == []
    assert ledger.list(account_ref='unknown')['performance'] == 'not_evaluated'
    for kwargs in ({'limit': True}, {'offset': -1}, {'limit': 1001}, {'kind': []}, {'broker': []}):
        with pytest.raises(ValueError):
            ledger.list(**kwargs)
    with pytest.raises(ValueError):
        ledger.get('unknown')


def test_cli_api_and_backup_preserve_all_events(tmp_path, capsys):
    from tradingagents.desktop.backup import create_backup, restore_backup

    store = DesktopStore(tmp_path / 'workspace')
    prefix = ['--workspace', str(store.directory)]
    path = tmp_path / 'event.json'
    path.write_text(json.dumps(payload()), encoding='utf-8-sig')
    account_events.main([*prefix, 'record', 'cash_transfer', str(path)])
    saved = json.loads(capsys.readouterr().out)
    account_events.main([*prefix, 'get', saved['record_id']])
    assert json.loads(capsys.readouterr().out)['raw'] == payload()
    account_events.main([*prefix, 'list', '--account-ref', 'synthetic-account'])
    assert json.loads(capsys.readouterr().out)['total'] == 1
    api = DesktopAPI(store)
    assert api.get_account_event(saved['record_id'])['ok']
    assert api.record_account_event('split', payload('split', event_id='split'))['ok']
    assert not api.record_account_event('cash_transfer', payload(amount='0'))['ok']
    assert not api.list_account_events(limit=0)['ok']
    archive = tmp_path / 'backup.zip'
    create_backup(store.directory, archive)
    restore_backup(archive, tmp_path / 'restored')
    assert AccountEventLedger(DesktopStore(tmp_path / 'restored')).list() == api.list_account_events()[
        'data']
    for content in (b'{', b'\xff', b'0' * 65537):
        path.write_bytes(content)
        with pytest.raises(SystemExit) as caught:
            account_events.main([*prefix, 'record', 'cash_transfer', str(path)])
        assert caught.value.code == 1
    assert api.list_account_events()['data']['total'] == 2


def test_cli_refuses_unknown_running_or_incomplete_workspace(tmp_path):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock

    path = tmp_path / 'workspace'
    prefix = ['--workspace', str(path), 'list']
    with pytest.raises(SystemExit):
        account_events.main(prefix)
    assert not path.exists()
    DesktopStore(path)
    with WorkspaceLock(path), pytest.raises(SystemExit):
        account_events.main(prefix)
    (path / INCOMPLETE).touch()
    with pytest.raises(SystemExit):
        account_events.main(prefix)
