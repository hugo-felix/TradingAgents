import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_desktop_strategies import complete_definition
from tests.test_desktop_trades import document, fill, ingest
from tradingagents.desktop.reviews import ReviewRegistry
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import StrategyRegistry
from tradingagents.desktop.trade_contexts import TradeContextRegistry
from tradingagents.desktop.trades import TradeLedger


def setup(tmp_path):
    store = DesktopStore(tmp_path)
    ledger = TradeLedger(store)
    ingest(ledger, document(fill()))
    return store, ledger.list_fills()['items'][0]['trade_id']


def notes(**changes):
    return {'research': None, 'strategy': '尚未评估规则有效性',
            'execution': '个人判断，尚无完整计划证据',
            'uncertainty': '单笔结果无法建立因果', **changes}


def test_review_snapshot_is_immutable_and_dimensions_remain_unknown(tmp_path):
    store, trade_id = setup(tmp_path)
    context = TradeContextRegistry(store).record(trade_id, {'personal_reason': '原始说明'})
    registry = ReviewRegistry(store)
    supplied = notes()
    saved = registry.record(trade_id, supplied, 'review-1')
    original = registry.get(saved['review_id'])
    supplied['research'] = 'changed'
    saved['notes']['research'] = 'changed'
    TradeContextRegistry(store).record(trade_id, {}, expected_version=1)
    assert registry.get(original['review_id']) == original
    assert original['context_snapshot'] == context
    assert original['notes']['research'] is None
    assert original['assessment'] == 'personal_opinion'
    assert original['causation'] == original['historical_knowledge'] == 'not_established'
    assert registry.record(trade_id, notes(), 'review-1') == original
    with pytest.raises(ValueError):
        registry.record(trade_id, notes(execution='different'), 'review-1')
    assert registry.record(trade_id, notes(), 'review-2')['review_id'] != original['review_id']
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM holdings').fetchone()[0] == 0


@pytest.mark.parametrize('invalid', [{}, {**notes(), 'outcome': 'wrong'},
                                    notes(research=0), notes(uncertainty=None),
                                    notes(uncertainty=''), notes(execution='a\nb')])
def test_invalid_notes_never_persist(tmp_path, invalid):
    store, trade_id = setup(tmp_path)
    registry = ReviewRegistry(store)
    with pytest.raises(ValueError):
        registry.record(trade_id, invalid, 'bad')
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM personal_trade_reviews').fetchone()[0] == 0


def test_rule_draft_requires_explicit_edit_and_confirmation_and_keeps_old_versions(tmp_path):
    store, trade_id = setup(tmp_path)
    registry = ReviewRegistry(store)
    review = registry.record(trade_id, notes(), 'review')
    definition = complete_definition()
    draft = registry.create_draft(review['review_id'], definition)
    assert draft['status'] == 'draft' and draft['confirmation'] is None
    assert draft['definition']['review_origin']['snapshot'] == review
    assert definition == complete_definition()
    strategies = StrategyRegistry(store)
    for rows in draft['definition']['rules'].values():
        assert all(row['kind'] == 'personal' and row['state'] == 'pending' for row in rows)
        assert all(review['review_id'] in row['citation'] for row in rows)
    with pytest.raises(ValueError):
        strategies.confirm(draft['strategy_id'], expected_version=1, confirmed_by='tester')
    with pytest.raises(ValueError):
        registry.create_draft(review['review_id'], definition)
    for rows in draft['definition']['rules'].values():
        for row in rows:
            row['state'] = 'manual'
    edited = strategies.save(draft['definition'], strategy_id=draft['strategy_id'], expected_version=1)
    confirmed = strategies.confirm(edited['strategy_id'], expected_version=2, confirmed_by='tester')
    assert confirmed['status'] == 'confirmed'
    assert confirmed['implementation'] == 'not_implemented'
    assert strategies.get(draft['strategy_id'], 1)['status'] == 'draft'
    assert registry.get(review['review_id']) == review


def test_missing_records_empty_rules_and_concurrent_draft_creation(tmp_path):
    store, trade_id = setup(tmp_path)
    registry = ReviewRegistry(store)
    with pytest.raises(ValueError):
        registry.record('missing', notes(), 'unknown')
    with pytest.raises(ValueError):
        registry.get('missing')
    review = registry.record(trade_id, notes(), 'review')
    with pytest.raises(ValueError):
        registry.create_draft(review['review_id'], {'name': 'empty'})

    def create(_):
        try:
            return ReviewRegistry(store).create_draft(review['review_id'], complete_definition())
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(StrategyRegistry(store).list_strategies()) == 1


def test_api_restart_and_backup_preserve_review_and_draft(tmp_path):
    from tradingagents.desktop.api import DesktopAPI
    from tradingagents.desktop.backup import create_backup, restore_backup

    store, trade_id = setup(tmp_path / 'workspace')
    api = DesktopAPI(store)
    review = api.record_trade_review(trade_id, notes(), 'review')['data']
    assert api.get_trade_review(review['review_id'])['data'] == review
    draft = api.create_review_strategy(review['review_id'], complete_definition())['data']
    assert not api.record_trade_review('missing', notes(), 'other')['ok']
    backup = tmp_path / 'snapshot.zip'
    create_backup(tmp_path / 'workspace', backup)
    destination = tmp_path / 'restored'
    restore_backup(backup, destination)
    restored = ReviewRegistry(DesktopStore(destination))
    assert restored.get(review['review_id']) == review
    assert restored.strategies.get(draft['strategy_id']) == draft


def test_review_queries_filter_and_page_in_append_order(tmp_path):
    from tradingagents.desktop.api import DesktopAPI

    store, trade_id = setup(tmp_path)
    ledger = TradeLedger(store)
    ingest(ledger, document(fill(execution_id='other')))
    other = next(row['trade_id'] for row in ledger.list_fills()['items']
                 if row['trade_id'] != trade_id)
    registry = ReviewRegistry(store)
    first = registry.record(trade_id, notes(), 'first')
    registry.record(other, notes(), 'other')
    last = registry.record(trade_id, notes(), 'last')
    assert registry.list(trade_id, limit=1) == {'total': 2, 'items': [last]}
    assert registry.list(trade_id, limit=1, offset=1) == {'total': 2, 'items': [first]}
    assert registry.list(offset=3) == {'total': 3, 'items': []}
    assert registry.list('unknown') == {'total': 0, 'items': []}
    api = DesktopAPI(store)
    assert api.list_trade_reviews(trade_id, 1, 1)['data']['items'] == [first]
    for kwargs in ({'limit': True}, {'limit': 1001}, {'offset': -1}, {'trade_id': []}):
        with pytest.raises(ValueError):
            registry.list(**kwargs)
    assert not api.list_trade_reviews(limit=0)['ok']


def test_concurrent_review_records_do_not_duplicate_or_overwrite(tmp_path):
    store, trade_id = setup(tmp_path)
    registry = ReviewRegistry(store)

    def record(research):
        try:
            return registry.record(trade_id, notes(research=research), 'concurrent')
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(record, ['first opinion', 'different opinion']))
    assert sum(row is not None for row in results) == 1
    original = registry.list()['items'][0]
    assert registry.list()['total'] == 1
    assert original in results
    with ThreadPoolExecutor(2) as pool:
        retries = list(pool.map(lambda _: registry.record(trade_id, original['notes'], 'concurrent'),
                                range(2)))
    assert retries == [original, original]


def test_review_cli_record_find_and_create_draft(tmp_path, capsys):
    from tradingagents.desktop.reviews import main

    store, trade_id = setup(tmp_path / 'workspace')
    prefix = ['--workspace', str(store.directory)]
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(notes()), encoding='utf-8-sig')
    main([*prefix, 'record', trade_id, str(source), '--request-id', 'cli'])
    review = json.loads(capsys.readouterr().out)
    main([*prefix, 'get', review['review_id']])
    assert json.loads(capsys.readouterr().out) == review
    main([*prefix, 'list', '--trade-id', trade_id, '--limit', '1'])
    assert json.loads(capsys.readouterr().out) == {'total': 1, 'items': [review]}
    source.write_text(json.dumps(complete_definition()), encoding='utf-8')
    main([*prefix, 'draft', review['review_id'], str(source)])
    draft = json.loads(capsys.readouterr().out)
    assert draft['status'] == 'draft'
    assert draft['definition']['review_origin']['snapshot'] == review
    with pytest.raises(SystemExit) as caught:
        main([*prefix, 'draft', review['review_id'], str(source)])
    assert caught.value.code == 1
    for content in (b'{', b'\xff', b'0' * 65537):
        source.write_bytes(content)
        with pytest.raises(SystemExit) as caught:
            main([*prefix, 'record', trade_id, str(source), '--request-id', 'bad'])
        assert caught.value.code == 1
    assert ReviewRegistry(store).list()['total'] == 1


def test_review_cli_refuses_unknown_running_and_incomplete_workspace(tmp_path):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock
    from tradingagents.desktop.reviews import main

    workspace = tmp_path / 'workspace'
    args = ['--workspace', str(workspace), 'list']
    with pytest.raises(SystemExit):
        main(args)
    assert not workspace.exists()
    DesktopStore(workspace)
    with WorkspaceLock(workspace), pytest.raises(SystemExit):
        main(args)
    (workspace / INCOMPLETE).touch()
    with pytest.raises(SystemExit):
        main(args)
