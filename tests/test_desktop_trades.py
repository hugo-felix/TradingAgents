import csv
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.desktop import trades
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.trades import COLUMNS, TradeLedger


def fill(**changes):
    return {'broker': 'synthetic-broker', 'account_ref': 'synthetic-account',
            'execution_id': 'fill-1', 'trade_date': '2024-01-02',
            'executed_at': '2024-01-02T10:15:00+08:00', 'ticker': '600941',
            'market': 'CN-A', 'side': 'buy', 'quantity': '100', 'price': '65.1250',
            'currency': 'CNY', 'fees': '', **changes}


def document(*rows):
    output = io.StringIO(newline='')
    writer = csv.DictWriter(output, COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def ingest(ledger, text, source='synthetic.csv'):
    preview = ledger.preview(text, source)
    return ledger.import_csv(text, source, preview['sha256'])


def test_preview_has_no_fills_and_import_preserves_originals_without_changing_holdings(tmp_path):
    store = DesktopStore(tmp_path)
    with store.connect() as db:
        db.execute('INSERT INTO holdings VALUES (?, ?, ?)', ('600941.SS', 7, 12.3))
        db.execute('INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)',
                   ('note', '600941.SS', None, 'personal', 'hold', '合成历史说明', '2023'))
    ledger = TradeLedger(store)
    text = '\ufeff' + document(fill(quantity='0.125000', fees='0'))
    preview = ledger.preview(text, '合成导入.csv')
    assert preview['new'] == preview['rows'] == 1
    assert ledger.list_fills()['total'] == 0
    assert preview['items'][0]['raw']['quantity'] == '0.125000'
    result = ledger.import_csv(text, '合成导入.csv', preview['sha256'])
    assert result['added'] == 1 and result['duplicates'] == 0
    restored = TradeLedger(DesktopStore(tmp_path))
    batch = restored.get_import(result['batch_id'])
    assert batch['csv_text'] == text
    assert hashlib.sha256(batch['csv_text'].encode()).hexdigest() == batch['sha256']
    saved = restored.list_fills()['items'][0]
    assert saved['kind'] == 'actual' and saved['verification'] == 'caller_declared_unverified'
    assert saved['fill']['ticker'] == '600941.SS'
    assert saved['fill']['quantity'] == '0.125' and saved['fill']['price'] == '65.125'
    assert saved['fill']['fees'] == '0'
    assert saved['fill']['executed_at'] == '2024-01-02T02:15:00+00:00'
    with store.connect() as db:
        assert tuple(db.execute('SELECT * FROM holdings').fetchone()) == ('600941.SS', 7, 12.3)
        assert db.execute('SELECT text FROM records').fetchone()[0] == '合成历史说明'


def test_same_batch_and_same_fills_across_files_are_idempotent(tmp_path):
    ledger = TradeLedger(DesktopStore(tmp_path))
    text = document(fill(), fill(), fill(execution_id='fill-2', fees='1.50'))
    first = ingest(ledger, text)
    assert (first['added'], first['duplicates']) == (2, 1)
    repeated = ingest(ledger, text)
    assert repeated == {**first, 'already_imported': True}
    second = ingest(ledger, document(fill(price='65.125', quantity='0100.00')), 'different.csv')
    assert second['added'] == 0 and second['duplicates'] == 1
    assert second['batch_id'] != first['batch_id']
    assert ledger.list_fills()['total'] == 2
    assert ledger.get_import(second['batch_id'])['rows'][0]['raw']['quantity'] == '0100.00'


def test_conflicts_roll_back_entire_batch_and_preserve_saved_economics(tmp_path):
    ledger = TradeLedger(DesktopStore(tmp_path))
    original = ingest(ledger, document(fill()))
    text = document(fill(execution_id='new-fill'), fill(quantity='200'))
    preview = ledger.preview(text, 'conflict.csv')
    assert preview['new'] == 1 and len(preview['conflicts']) == 1
    with pytest.raises(ValueError, match='整批未导入'):
        ledger.import_csv(text, 'conflict.csv', preview['sha256'])
    assert ledger.list_fills()['total'] == 1
    assert ledger.list_fills()['items'][0]['fill']['quantity'] == '100'
    assert ledger.get_import(original['batch_id'])['added'] == 1
    with pytest.raises(ValueError, match='同一批'):
        ingest(ledger, document(fill(), fill(price='999')))
    with ledger.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM actual_fill_imports').fetchone()[0] == 1


def test_identity_distinguishes_brokers_accounts_dates_and_partial_fills(tmp_path):
    ledger = TradeLedger(DesktopStore(tmp_path))
    rows = [fill(), fill(broker='second'), fill(account_ref='second'),
            fill(execution_id='second'),
            fill(trade_date='2024-01-03', executed_at='2024-01-03T10:15:00+08:00')]
    assert ingest(ledger, document(*rows))['added'] == 5
    assert ledger.list_fills()['total'] == 5


def test_missing_fee_does_not_become_zero_and_fee_correction_is_a_conflict(tmp_path):
    ledger = TradeLedger(DesktopStore(tmp_path))
    assert ledger.preview(document(fill()), 'synthetic')['missing_fees'] == 1
    ingest(ledger, document(fill()))
    assert ledger.list_fills()['items'][0]['fill']['fees'] is None
    preview = ledger.preview(document(fill(fees='0')), 'synthetic')
    assert len(preview['conflicts']) == 1


@pytest.mark.parametrize(('field', 'value'), [
    ('broker', ''), ('account_ref', ' '), ('execution_id', ''), ('market', ''),
    ('quantity', '0'), ('quantity', '-1'), ('quantity', 'NaN'), ('quantity', '1e3'),
    ('price', '0'), ('price', 'inf'), ('price', '1,000'), ('fees', '-1'),
    ('fees', 'NaN'), ('side', 'planned'), ('side', 'BUY'), ('currency', 'cny'),
    ('currency', 'USDT'), ('executed_at', '2024-01-02T10:15:00'),
    ('executed_at', 'invalid'), ('trade_date', '20240102'), ('trade_date', '2024-01-03'),
    ('ticker', "A'; DROP TABLE holdings;"), ('broker', 'line\nbreak'),
])
def test_invalid_rows_never_publish_a_batch(tmp_path, field, value):
    ledger = TradeLedger(DesktopStore(tmp_path))
    with pytest.raises(ValueError):
        ingest(ledger, document(fill(**{field: value})))
    assert ledger.list_fills()['total'] == 0


@pytest.mark.parametrize('text', [
    '', ','.join(COLUMNS) + '\n', 'ticker,quantity\nAAPL,1\n',
    ','.join(COLUMNS) + '\nonly-one-column\n',
    ','.join(COLUMNS) + '\n' + ','.join(['x'] * 13) + '\n',
    ','.join(COLUMNS[:-1] + ('ticker',)) + '\n',
])
def test_missing_extra_and_duplicate_headers_or_cells_are_rejected(tmp_path, text):
    with pytest.raises(ValueError):
        ingest(TradeLedger(DesktopStore(tmp_path)), text)


def test_changed_fingerprint_limits_and_missing_batch(tmp_path, monkeypatch):
    ledger = TradeLedger(DesktopStore(tmp_path))
    text = document(fill())
    with pytest.raises(ValueError, match='重新预览'):
        ledger.import_csv(text, 'synthetic', '0' * 64)
    with monkeypatch.context() as scoped:
        scoped.setattr(trades, 'MAX_BYTES', 1)
        with pytest.raises(ValueError, match='MiB'):
            ledger.preview(text, 'synthetic')
    with monkeypatch.context() as scoped:
        scoped.setattr(trades, 'MAX_ROWS', 1)
        with pytest.raises(ValueError, match='10000'):
            ledger.preview(document(fill(), fill()), 'synthetic')
    with pytest.raises(ValueError, match='未找到'):
        ledger.get_import('missing')
    assert ledger.list_fills()['total'] == 0


def test_concurrent_imports_cannot_duplicate_fills(tmp_path):
    ledger = TradeLedger(DesktopStore(tmp_path))
    text = document(fill(), fill(execution_id='fill-2'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ingest(ledger, text), range(2)))
    assert sorted(row['already_imported'] for row in results) == [False, True]
    assert ledger.list_fills()['total'] == 2


def test_filter_pagination_exact_decimals_and_time_ordering(tmp_path):
    ledger = TradeLedger(DesktopStore(tmp_path))
    large = '123456789012345678901234567890.123456789'
    ingest(ledger, document(fill(price=large), fill(execution_id='later', ticker='AAPL',
           executed_at='2024-01-02T02:15:00.000001+00:00', market='US', side='sell')))
    assert ledger.list_fills(limit=1)['items'][0]['fill']['ticker'] == 'AAPL'
    assert ledger.list_fills(limit=1, offset=1)['items'][0]['fill']['price'] == large
    assert ledger.list_fills('600941')['total'] == 1
    assert ledger.list_fills('MSFT')['total'] == 0
    for kwargs in ({'limit': True}, {'limit': 0}, {'limit': 1001}, {'offset': -1}):
        with pytest.raises(ValueError):
            ledger.list_fills(**kwargs)


def test_bridge_preview_import_and_errors(tmp_path):
    from tradingagents.desktop.api import DesktopAPI

    api = DesktopAPI(DesktopStore(tmp_path))
    text = document(fill())
    preview = api.preview_actual_trades(text, 'synthetic.csv')['data']
    assert not api.import_actual_trades(text, 'synthetic.csv', 'changed')['ok']
    batch = api.import_actual_trades(text, 'synthetic.csv', preview['sha256'])['data']
    assert api.get_trade_import(batch['batch_id'])['data']['csv_text'] == text
    assert api.list_actual_trades()['data']['total'] == 1
    assert not api.preview_actual_trades(text, '')['ok']


def test_backup_restore_preserves_fill_and_source_audit(tmp_path):
    from tradingagents.desktop.backup import create_backup, restore_backup

    store = DesktopStore(tmp_path / 'source')
    batch = ingest(TradeLedger(store), document(fill()))
    archive = tmp_path / 'backup.zip'
    create_backup(store.directory, archive)
    restore_backup(archive, tmp_path / 'restored')
    restored = TradeLedger(DesktopStore(tmp_path / 'restored'))
    assert restored.get_import(batch['batch_id']) == {
        key: value for key, value in batch.items() if key != 'already_imported'}
    assert restored.list_fills()['total'] == 1


def test_cli_preview_import_and_audit(tmp_path, capsys):
    import json

    store = DesktopStore(tmp_path / 'workspace')
    path = tmp_path / 'synthetic.csv'
    text = document(fill())
    path.write_text(text, encoding='utf-8', newline='')
    prefix = ['--workspace', str(store.directory)]
    trades.main([*prefix, 'preview', str(path), '--source', 'synthetic'])
    preview = json.loads(capsys.readouterr().out)
    assert preview['new'] == 1
    assert TradeLedger(store).list_fills()['total'] == 0
    trades.main([*prefix, 'import', str(path), '--source', 'synthetic',
                 '--sha256', preview['sha256']])
    batch = json.loads(capsys.readouterr().out)
    assert batch['added'] == 1 and 'csv_text' not in batch
    trades.main([*prefix, 'batch', batch['batch_id']])
    assert json.loads(capsys.readouterr().out)['csv_text'] == text
    trades.main([*prefix, 'list', '--ticker', '600941', '--limit', '1'])
    assert json.loads(capsys.readouterr().out)['total'] == 1


def test_cli_refuses_wrong_workspace_running_and_incomplete_restore(tmp_path, capsys):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock

    missing = tmp_path / 'missing'
    with pytest.raises(SystemExit) as caught:
        trades.main(['--workspace', str(missing), 'list'])
    assert caught.value.code == 1 and not missing.exists()
    store = DesktopStore(tmp_path / 'workspace')
    prefix = ['--workspace', str(store.directory), 'list']
    with WorkspaceLock(store.directory), pytest.raises(SystemExit):
        trades.main(prefix)
    marker = store.directory / INCOMPLETE
    marker.write_text('incomplete', encoding='utf-8')
    with pytest.raises(SystemExit):
        trades.main(prefix)
    assert '未完整恢复' in capsys.readouterr().err


def test_cli_bad_encoding_and_changed_input_do_not_import(tmp_path, capsys):
    store = DesktopStore(tmp_path / 'workspace')
    path = tmp_path / 'synthetic.csv'
    prefix = ['--workspace', str(store.directory), 'import', str(path),
              '--source', 'synthetic', '--sha256', '0' * 64]
    path.write_bytes(b'\xff\xfe')
    with pytest.raises(SystemExit):
        trades.main(prefix)
    path.write_text(document(fill()), encoding='utf-8')
    with pytest.raises(SystemExit):
        trades.main(prefix)
    assert '重新预览' in capsys.readouterr().err
    assert TradeLedger(store).list_fills()['total'] == 0
