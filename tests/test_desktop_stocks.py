import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.instruments import canonical_symbol, stock_identity
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.tasks import validate_request


@pytest.mark.parametrize('value', ['601658', '601658.SH', '601658.SS', 'sh601658'])
def test_bare_and_qualified_a_share_codes_share_one_identity(value):
    identity = stock_identity(value)
    assert identity['ticker'] == '601658.SS'
    assert identity['name'] == '邮储银行'
    assert identity['resolved']


def test_names_come_from_directory_and_unknown_stays_unresolved():
    assert stock_identity('600941')['name'] == '中国移动'
    assert stock_identity('000001')['ticker'] == '000001.SZ'
    assert stock_identity('688981')['ticker'] == '688981.SS'
    assert not stock_identity('999999')['resolved']
    assert canonical_symbol('AAPL') == 'AAPL'
    assert canonical_symbol('邮储银行') == '601658.SS'


def test_holdings_and_history_gain_names_without_overwriting_legacy_amounts(tmp_path):
    store = DesktopStore(tmp_path)
    with store.connect() as db:
        db.execute('INSERT INTO holdings VALUES (?, ?, ?)', ('601658', 100, 3.1265))
        db.execute('INSERT INTO records VALUES (?, ?, NULL, ?, ?, ?, ?)',
                   ('legacy', '601658', 'holding', 'snapshot', '保留原始记录', '2026-11-06'))
    api = DesktopAPI(store)
    data = api.bootstrap()['data']
    assert data['holdings'][0]['name'] == '邮储银行'
    assert data['holdings'][0]['quantity'] == 100
    assert api.history_entries('邮储银行')['data'][0]['name'] == '邮储银行'
    assert api.history_entries('601658.SS')['data'][0]['text'] == '保留原始记录'
    api.save_holding('601658.SS', 100, 3.1265, '再次核对')
    assert len(store.holdings()) == 1
    assert len(store.history()) == 1


def test_ambiguous_legacy_positions_are_not_silently_merged(tmp_path):
    store = DesktopStore(tmp_path)
    with store.connect() as db:
        db.execute('INSERT INTO holdings VALUES (?, ?, ?)', ('601658', 100, 3.1265))
        db.execute('INSERT INTO holdings VALUES (?, ?, ?)', ('601658.SS', 200, 4))
    with pytest.raises(ValueError, match='重复'):
        store.save_holding('601658.SS', 100, 3, '核对')
    assert sum(row['quantity'] for row in store.holdings()) == 300


def test_bridge_resolves_code_without_network_or_model_call(tmp_path):
    api = DesktopAPI(DesktopStore(tmp_path))
    result = api.resolve_stock('601658')
    assert result['ok']
    assert result['data']['name'] == '邮储银行'
    request = validate_request({'ticker': '601658', 'date': '2026-11-05', 'provider': 'openai',
                                'quick_model': 'test', 'deep_model': 'test'})
    assert request['ticker'] == '601658.SS'
