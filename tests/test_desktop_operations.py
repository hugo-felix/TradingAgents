import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_desktop_trades import document, fill, ingest
from tradingagents.desktop import operations
from tradingagents.desktop.operations import OperationLedger
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.trades import TradeLedger


def payload(kind='plan', **changes):
    result = {'book_ref': 'synthetic-virtual-book', 'ticker': '600941', 'market': 'CN-A',
              'side': 'buy', 'quantity': '0100.000', 'currency': 'CNY',
              'source_label': 'synthetic-personal-input'}
    if kind == 'plan':
        result.update(planned_for='2024-01-02T10:15:00+08:00', limit_price=None,
                      reason='合成计划理由')
    else:
        result.update(simulated_at='2024-01-02T10:15:00+08:00', price='65.125', fees=None,
                      assumptions='合成假设：忽略滑点，未核验涨跌停或成交量')
    return {**result, **changes}


def test_three_kinds_are_independent_immutable_and_not_holdings(tmp_path):
    store = DesktopStore(tmp_path)
    actual = TradeLedger(store)
    ingest(actual, document(fill()))
    before = actual.list_fills()
    ledger = OperationLedger(store)
    source = payload()
    plan = ledger.record('plan', source, 'same-request')
    simulated = ledger.record('simulation', payload('simulation'), 'same-request')
    assert plan['kind'] == 'plan' and plan['execution'] == 'not_executed'
    assert simulated['kind'] == 'simulation' and simulated['execution'] == 'simulated_only'
    assert simulated['payload']['fees'] is None
    assert plan['payload']['limit_price'] is None
    assert plan['payload']['quantity'] == '100'
    assert plan['payload']['planned_for'] == '2024-01-02T02:15:00+00:00'
    assert plan['historical_knowledge'] == 'not_established'
    assert ledger.list('plan', '600941')['items'][0]['operation_id'] == plan['operation_id']
    assert ledger.list('simulation')['total'] == 1
    source['quantity'] = '999'
    plan['payload']['quantity'] = '999'
    assert ledger.get(plan['operation_id'])['payload']['quantity'] == '100'
    assert actual.list_fills() == before
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM holdings').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM reports').fetchone()[0] == 0
    for operation in (plan, simulated):
        with pytest.raises(ValueError):
            actual.get_fill(operation['operation_id'])
    with pytest.raises(ValueError):
        ledger.get(before['items'][0]['trade_id'])


def test_retries_normalization_and_conflicts_preserve_original(tmp_path):
    ledger = OperationLedger(DesktopStore(tmp_path))
    original = ledger.record('simulation', payload('simulation', fees='0'), 'id')
    repeated = ledger.record('simulation', payload('simulation', fees='0.00', quantity='100'), 'id')
    assert repeated == {**original, 'already_recorded': True}
    for changed in ({'price': '66'}, {'fees': None}, {'source_label': 'other'}):
        with pytest.raises(ValueError, match='不能覆盖'):
            ledger.record('simulation', {**payload('simulation', fees='0'), **changed}, 'id')
    assert ledger.list()['total'] == 1
    assert ledger.get(original['operation_id']) == {
        k: v for k, v in original.items() if k != 'already_recorded'}


@pytest.mark.parametrize(('kind', 'changes'), [
    ('actual', {}), ('plan', {'kind': 'actual'}), ('plan', {'quantity': 100}),
    ('plan', {'quantity': '0'}), ('plan', {'quantity': 'NaN'}),
    ('plan', {'planned_for': '2024-01-01T00:00:00'}), ('plan', {'planned_for': 'bad'}),
    ('plan', {'limit_price': '0'}), ('plan', {'reason': ''}),
    ('plan', {'currency': 'cny'}), ('plan', {'side': 'hold'}),
    ('simulation', {'assumptions': ''}), ('simulation', {'assumptions': 'x' * 10001}),
    ('simulation', {'fees': '-1'}), ('simulation', {'fees': 0}),
    ('simulation', {'price': None}), ('simulation', {'execution_id': 'pretend-real'}),
])
def test_invalid_entries_do_not_write(tmp_path, kind, changes):
    ledger = OperationLedger(DesktopStore(tmp_path))
    with pytest.raises(ValueError):
        ledger.record(kind, {**payload(kind), **changes}, 'bad')
    assert ledger.list()['total'] == 0


def test_concurrent_requests_are_atomic(tmp_path):
    ledger = OperationLedger(DesktopStore(tmp_path))

    def record(quantity):
        try:
            return ledger.record('plan', payload(quantity=quantity), 'concurrent')
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(record, ['100', '200']))
    assert sum(row is not None for row in results) == 1
    assert ledger.list()['total'] == 1
    with ThreadPoolExecutor(2) as pool:
        retries = list(pool.map(lambda _: ledger.record('plan', payload(), 'retries'), range(2)))
    assert {row['already_recorded'] for row in retries} == {True, False}
    for kwargs in ({'kind': 'actual'}, {'kind': []}, {'limit': True}, {'offset': -1}):
        with pytest.raises(ValueError):
            ledger.list(**kwargs)


def test_cli_api_and_backup(tmp_path, capsys):
    from tradingagents.desktop.api import DesktopAPI
    from tradingagents.desktop.backup import create_backup, restore_backup

    store = DesktopStore(tmp_path / 'workspace')
    prefix = ['--workspace', str(store.directory)]
    path = tmp_path / 'plan.json'
    path.write_text(json.dumps(payload()), encoding='utf-8')
    operations.main([*prefix, 'record', 'plan', str(path), '--request-id', 'cli'])
    saved = json.loads(capsys.readouterr().out)
    operations.main([*prefix, 'get', saved['operation_id']])
    assert json.loads(capsys.readouterr().out)['payload'] == saved['payload']
    operations.main([*prefix, 'list', '--kind', 'simulation'])
    assert json.loads(capsys.readouterr().out) == {'total': 0, 'items': []}
    api = DesktopAPI(store)
    assert api.get_operation(saved['operation_id'])['ok']
    assert api.record_operation('simulation', payload('simulation'), 'api')['ok']
    assert not api.record_operation('actual', payload(), 'api')['ok']
    assert api.list_operations()['data']['total'] == 2
    archive = tmp_path / 'backup.zip'
    create_backup(store.directory, archive)
    restore_backup(archive, tmp_path / 'restored')
    assert OperationLedger(DesktopStore(tmp_path / 'restored')).list() == api.list_operations()['data']
    for content in (b'{', b'\xff', b'0' * 65537):
        path.write_bytes(content)
        with pytest.raises(SystemExit) as caught:
            operations.main([*prefix, 'record', 'plan', str(path), '--request-id', 'bad'])
        assert caught.value.code == 1
    assert api.list_operations()['data']['total'] == 2


def test_cli_refuses_unknown_running_or_incomplete_workspace(tmp_path):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock

    path = tmp_path / 'workspace'
    prefix = ['--workspace', str(path), 'list']
    with pytest.raises(SystemExit):
        operations.main(prefix)
    assert not path.exists()
    DesktopStore(path)
    with WorkspaceLock(path), pytest.raises(SystemExit):
        operations.main(prefix)
    (path / INCOMPLETE).touch()
    with pytest.raises(SystemExit):
        operations.main(prefix)
