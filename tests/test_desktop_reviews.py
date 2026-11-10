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
