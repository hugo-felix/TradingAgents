import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_desktop_strategies import complete_definition
from tests.test_desktop_trades import document, fill, ingest
from tradingagents.desktop import strategies, trades
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import StrategyRegistry
from tradingagents.desktop.trade_contexts import TradeContextRegistry
from tradingagents.desktop.trades import TradeLedger


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = DesktopStore(tmp_path / 'workspace')
    ledger = TradeLedger(store)
    ingest(ledger, document(fill()))
    trade = ledger.list_fills()['items'][0]
    monkeypatch.setattr(strategies, 'now', lambda: '2024-01-01T12:00:00+08:00')
    registry = StrategyRegistry(store)
    draft = registry.save(complete_definition())
    strategy = registry.confirm(draft['strategy_id'], expected_version=1, confirmed_by='synthetic')
    text = '{ "id": "report-1", "original": "合成原文，不重新生成" }'
    with store.connect() as db:
        db.execute('INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?)',
                   ('report-1', '600941.SS', '2024-01-01', 'hold',
                    '2024-01-01T15:00:00+08:00', text))
        db.execute('INSERT INTO holdings VALUES (?, ?, ?)', ('600941.SS', 7, 12.3))
        db.execute('INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)',
                   ('note', '600941.SS', None, 'personal', 'hold', '原说明', '2023'))
    return store, trade, strategy, text


def links(strategy, **changes):
    return {'personal_reason': '个人补录假设，不证明当时已持有此判断', 'report_id': 'report-1',
            'strategy_id': strategy['strategy_id'], 'strategy_version': strategy['version'],
            **changes}


def test_immutable_snapshots_revisions_and_no_original_state_changes(setup, monkeypatch):
    store, trade, strategy, text = setup
    monkeypatch.setattr(store, 'get_report', lambda *a: pytest.fail('must read original snapshot'))
    contexts = TradeContextRegistry(store)
    request = links(strategy)
    first = contexts.record(trade['trade_id'], request)
    assert first['report']['document_text'] == text
    assert first['report']['sha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert first['report']['availability'] == 'recorded_before_trade'
    assert first['strategy']['document'] == strategy
    assert first['usage'] == 'personal_review_only'
    assert first['knowledge_at_trade'] == 'not_established'
    assert first['recorded_at'] != trade['fill']['executed_at']
    request['personal_reason'] = 'caller mutation'
    first['strategy']['document']['definition']['name'] = 'caller mutation'
    registry = StrategyRegistry(store)
    registry.save({'name': 'new draft'}, strategy_id=strategy['strategy_id'], expected_version=2)
    second = contexts.record(trade['trade_id'], {'personal_reason': '纠正理由，旧版本仍保存'},
                             expected_version=1)
    assert second['version'] == 2 and second['previous_version'] == 1
    assert second['report'] is second['strategy'] is None
    reopened = TradeContextRegistry(DesktopStore(store.directory))
    assert reopened.get(trade['trade_id']) == second
    saved = reopened.get(trade['trade_id'], 1)
    assert saved['strategy']['document'] == strategy
    assert saved['request']['personal_reason'] != 'caller mutation'
    assert reopened.history(trade['trade_id'], limit=1)['items'] == [second]
    assert reopened.history(trade['trade_id'], offset=1)['items'] == [saved]
    assert TradeLedger(store).get_fill(trade['trade_id']) == trade
    with store.connect() as db:
        assert db.execute('SELECT document FROM reports').fetchone()[0] == text
        assert tuple(db.execute('SELECT * FROM holdings').fetchone()) == ('600941.SS', 7, 12.3)
        assert db.execute('SELECT text FROM records').fetchone()[0] == '原说明'


def test_historical_unlinked_and_late_import_does_not_prove_availability(setup):
    store, trade, strategy, _ = setup
    contexts = TradeContextRegistry(store)
    assert contexts.get(trade['trade_id']) is None
    assert contexts.history(trade['trade_id']) == {'total': 0, 'items': []}
    empty = contexts.record(trade['trade_id'], {})
    assert all(value is None for value in empty['request'].values())
    with store.connect() as db:
        db.execute("UPDATE reports SET imported_at='2026-01-01T00:00:00+00:00'")
    late = contexts.record(trade['trade_id'], links(strategy), expected_version=1)
    assert late['report']['availability'] == 'not_established'
    assert late['knowledge_at_trade'] == 'not_established'


@pytest.mark.parametrize('payload', [None, [], {'unknown': 1}, {'personal_reason': ''},
    {'personal_reason': 'x' * 10001}, {'report_id': 1}, {'strategy_version': 2},
    {'strategy_id': 'id'}, {'strategy_id': 'id', 'strategy_version': True},
    {'strategy_id': 'id', 'strategy_version': 0}, {'strategy_id': 'id', 'strategy_version': '2'}])
def test_invalid_requests_do_not_save(setup, payload):
    store, trade, _, _ = setup
    contexts = TradeContextRegistry(store)
    with pytest.raises(ValueError):
        contexts.record(trade['trade_id'], payload)
    assert contexts.history(trade['trade_id'])['total'] == 0


@pytest.mark.parametrize('change,error', [
    ({'report_id': 'missing'}, '报告快照'),
    ({'strategy_id': 'missing'}, '策略版本'),
    ({'strategy_version': 1}, '已确认'),
    ({'strategy_version': 999}, '策略版本'),
])
def test_missing_and_unconfirmed_references_reject_atomically(setup, change, error):
    store, trade, strategy, _ = setup
    contexts = TradeContextRegistry(store)
    with pytest.raises(ValueError, match=error):
        contexts.record(trade['trade_id'], links(strategy, **change))
    assert contexts.get(trade['trade_id']) is None


@pytest.mark.parametrize('column,value,error', [
    ('date', '2024-01-03', '分析日期'), ('ticker', 'AAPL', '股票代码'),
    ('imported_at', '2024-01-01T00:00:00', '时区'),
])
def test_report_cutoff_ticker_and_unknown_timezone(setup, column, value, error):
    store, trade, strategy, _ = setup
    with store.connect() as db:
        db.execute(f'UPDATE reports SET {column}=?', (value,))  # fixed parametrized column names
    contexts = TradeContextRegistry(store)
    with pytest.raises(ValueError, match=error):
        contexts.record(trade['trade_id'], links(strategy))
    assert contexts.get(trade['trade_id']) is None


def test_future_strategy_and_confirmation_are_not_historical_basis(setup):
    store, trade, strategy, _ = setup
    contexts = TradeContextRegistry(store)
    for field in ('created_at', 'confirmation'):
        document = {**strategy, 'confirmation': dict(strategy['confirmation'])}
        if field == 'created_at':
            document['created_at'] = '2024-01-02T02:15:00.000001+00:00'
        else:
            document['confirmation']['at'] = '2024-01-02T02:15:00.000001+00:00'
        with store.connect() as db:
            db.execute('UPDATE strategy_versions SET created_at=?, document=? WHERE version=2',
                       (document['created_at'], json.dumps(document)))
        with pytest.raises(ValueError, match='成交后'):
            contexts.record(trade['trade_id'], links(strategy))
    assert contexts.history(trade['trade_id'])['total'] == 0


def test_stale_concurrent_writers_preserve_one_version(setup):
    store, trade, _, _ = setup
    contexts = TradeContextRegistry(store)

    def write(reason):
        try:
            return contexts.record(trade['trade_id'], {'personal_reason': reason})
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, ['synthetic-a', 'synthetic-b']))
    assert sum(value is not None for value in results) == 1
    assert contexts.history(trade['trade_id'])['total'] == 1
    with pytest.raises(ValueError, match='已更新'):
        contexts.record(trade['trade_id'], {})
    for version in (True, -1, '1'):
        with pytest.raises(ValueError):
            contexts.record(trade['trade_id'], {}, expected_version=version)
    for version in (False, 0, 99):
        with pytest.raises(ValueError):
            contexts.get(trade['trade_id'], version)
    for kwargs in ({'limit': True}, {'limit': 1001}, {'offset': -1}):
        with pytest.raises(ValueError):
            contexts.history(trade['trade_id'], **kwargs)
    for call in (contexts.get, contexts.history):
        with pytest.raises(ValueError, match='实际成交'):
            call('missing')
    with pytest.raises(ValueError, match='实际成交'):
        contexts.record('missing', {})


def test_cli_bridge_and_backup_roundtrip(setup, tmp_path, capsys):
    from tradingagents.desktop.api import DesktopAPI
    from tradingagents.desktop.backup import create_backup, restore_backup

    store, trade, strategy, _ = setup
    prefix = ['--workspace', str(store.directory)]
    trades.main([*prefix, 'get', trade['trade_id']])
    assert json.loads(capsys.readouterr().out) == trade
    trades.main([*prefix, 'context', trade['trade_id']])
    assert json.loads(capsys.readouterr().out) is None
    path = tmp_path / 'context.json'
    path.write_text(json.dumps(links(strategy)), encoding='utf-8')
    trades.main([*prefix, 'context-save', trade['trade_id'], str(path), '--expected-version', '0'])
    original = json.loads(capsys.readouterr().out)
    trades.main([*prefix, 'context-history', trade['trade_id'], '--limit', '1'])
    assert json.loads(capsys.readouterr().out) == {'total': 1, 'items': [original]}
    api = DesktopAPI(store)
    assert api.get_actual_trade(trade['trade_id'])['data'] == trade
    assert api.get_trade_context(trade['trade_id'])['data'] == original
    assert not api.record_trade_context(trade['trade_id'], {})['ok']
    second = api.record_trade_context(trade['trade_id'], {}, 1)['data']
    assert api.trade_context_history(trade['trade_id'])['data']['total'] == 2
    archive = tmp_path / 'backup.zip'
    create_backup(store.directory, archive)
    restore_backup(archive, tmp_path / 'restored')
    restored = TradeContextRegistry(DesktopStore(tmp_path / 'restored'))
    assert restored.get(trade['trade_id'], 1) == original
    assert restored.get(trade['trade_id']) == second
    for value in (b'{', b'\xff', b'0' * 65537):
        path.write_bytes(value)
        with pytest.raises(SystemExit) as caught:
            trades.main([*prefix, 'context-save', trade['trade_id'], str(path),
                         '--expected-version', '2'])
        assert caught.value.code == 1
    assert restored.history(trade['trade_id'])['total'] == 2


def test_no_strategy_table_or_annotations_required_for_historical_fill(tmp_path):
    store = DesktopStore(tmp_path)
    ledger = TradeLedger(store)
    ingest(ledger, document(fill()))
    trade = ledger.list_fills()['items'][0]
    contexts = TradeContextRegistry(store)
    with pytest.raises(ValueError, match='策略版本'):
        contexts.record(trade['trade_id'], {'strategy_id': 'missing', 'strategy_version': 1})
    assert contexts.get(trade['trade_id']) is None
    saved = contexts.record(trade['trade_id'], {})
    assert contexts.get(' ' + trade['trade_id'] + ' ') == saved
    assert contexts.history(' ' + trade['trade_id'] + ' ')['total'] == 1
