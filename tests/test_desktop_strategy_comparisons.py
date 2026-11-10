import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import RULE_FIELDS, SCOPE_FIELDS
from tradingagents.desktop.strategy_comparisons import StrategyComparisonRegistry, differences


def fixture(directory):
    registry = StrategyComparisonRegistry(DesktopStore(directory))
    definition = {**dict.fromkeys(SCOPE_FIELDS, '合成离线假设'), 'market': 'synthetic',
                  'timeframe': 'daily', 'rules': {
                      key: [{'text': '人工规则', 'citation': '测试假设', 'kind': 'personal',
                             'state': 'manual'}] for key in RULE_FIELDS}}
    data = {'symbol': 'SYNTHETIC', 'market': 'synthetic', 'timeframe': 'daily',
            'price_basis': 'raw', 'source': 'synthetic', 'revision': 'v1', 'series': {
                'close': {'unit': 'CNY/share', 'observations': [{
                    'observed_at': '2020-01-02T15:00:00+08:00',
                    'available_at': '2020-01-02T15:05:00+08:00', 'value': 30}]}}}
    checks = []
    for name, op in (('趋势 <script>假设</script>', 'gt'), ('价值假设', 'lt')):
        definition['name'] = name
        definition['rules']['entry'] = [{
            'text': '独立条件', 'citation': '个人测试假设', 'kind': 'personal', 'state': 'defined',
            'check': {'op': op, 'left': {'metric': 'close'},
                      'right': {'constant': 20, 'unit': 'CNY/share', 'source': '测试假设'}}}]
        draft = registry.checks.strategies.save(definition)
        strategy = registry.checks.strategies.confirm(draft['strategy_id'], expected_version=1,
                                                     confirmed_by='tester')
        checks.append(registry.checks.check(strategy['strategy_id'], 2,
                                           as_of='2020-01-02T16:00:00+08:00', data=data))
    return registry, checks[0], checks[1]


def test_independent_evidence_personal_verdict_and_immutable_versions(tmp_path):
    registry, left, right = fixture(tmp_path)
    args = (left['check_id'], right['check_id'], 'conflict_identified',
            '仅为个人测试：两条条件含义不同，需核对适用环境。', '测试人')
    preview = registry.preview(left['check_id'], right['check_id'])
    assert preview['entries'][0]['check'] == left
    assert preview['entries'][1]['check'] == right
    assert registry.history(left['check_id']) == []
    record = registry.save(*args)
    original = copy.deepcopy(record)
    assert record['context_differences'] == []
    assert record['entries'][0]['check']['counts'] == {'met': 1, 'manual': 6}
    assert record['entries'][1]['check']['counts'] == {'not_met': 1, 'manual': 6}
    assert not {'score', 'signal', 'passed', 'recommended_action'} & record.keys()
    strategy = record['entries'][0]['strategy']
    registry.checks.strategies.save(strategy['definition'], strategy_id=strategy['strategy_id'],
                                    expected_version=2)
    record['entries'][0]['strategy']['definition']['name'] = 'mutated'
    record['entries'][1]['check']['data']['source'] = 'mutated'
    reopened = StrategyComparisonRegistry(DesktopStore(tmp_path))
    assert reopened.get(original['comparison_id']) == original
    assert reopened.save(*args) == original
    assert reopened.checks.get(left['check_id']) == left
    assert reopened.checks.get(right['check_id']) == right
    for check in (left, right):
        history = reopened.history(check['check_id'])
        assert len(history) == 1
        assert history[0]['comparison_id'] == original['comparison_id']
        assert 'entries' not in history[0]
    changed = reopened.save(left['check_id'], right['check_id'], 'not_assessed', '', '测试人')
    assert changed['comparison_id'] != original['comparison_id']
    assert len(reopened.history(left['check_id'])) == 2


@pytest.mark.parametrize('field', ['symbol', 'market', 'timeframe', 'price_basis', 'source',
                                  'revision', 'as_of', 'series'])
def test_each_context_difference_is_explicit(tmp_path, field):
    _, left, right = fixture(tmp_path)
    if field == 'as_of':
        right[field] = '2020-01-02T16:01:00+08:00'
    elif field == 'series':
        right['data'][field]['close']['unit'] = 'USD/share'
    else:
        right['data'][field] = 'different'
    assert differences(left, right) == [field]


def test_equal_instant_different_timezone_is_not_a_time_difference(tmp_path):
    _, left, right = fixture(tmp_path)
    right['as_of'] = '2020-01-02T08:00:00+00:00'
    assert differences(left, right) == []


@pytest.mark.parametrize(('verdict', 'note', 'by'), [
    ('automatic', '', 'tester'), ([], '', 'tester'), ('conflict_identified', ' ', 'tester'),
    ('no_conflict_identified', '', 'tester'), ('not_assessed', None, 'tester'),
    ('not_assessed', 'x' * 10001, 'tester'), ('not_assessed', '', ''),
    ('not_assessed', '', 'x' * 201), ('not_assessed', '', None)])
def test_invalid_personal_assessment_leaves_no_record(tmp_path, verdict, note, by):
    registry, left, right = fixture(tmp_path)
    with pytest.raises(ValueError):
        registry.save(left['check_id'], right['check_id'], verdict, note, by)
    assert registry.history(left['check_id']) == []


def test_missing_duplicate_and_concurrent_retry(tmp_path):
    registry, left, right = fixture(tmp_path)
    for other in (left['check_id'], 'missing', None):
        with pytest.raises(ValueError):
            registry.save(left['check_id'], other, 'not_assessed', '', 'tester')
    assert registry.history(left['check_id']) == []
    args = (left['check_id'], right['check_id'], 'not_assessed', '', 'tester')
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(lambda _: registry.save(*args), range(6)))
    assert all(row == rows[0] for row in rows)
    assert len(registry.history(right['check_id'])) == 1
    with pytest.raises(ValueError, match='未找到'):
        registry.get('missing')
    with pytest.raises(ValueError, match='未找到'):
        registry.history('missing')


def test_desktop_bridge(tmp_path):
    registry, left, right = fixture(tmp_path)
    api = DesktopAPI(registry.store)
    try:
        assert api.preview_strategy_comparison(left['check_id'], right['check_id'])['ok']
        assert not api.preview_strategy_comparison(left['check_id'], 'missing')['ok']
        result = api.save_strategy_comparison(left['check_id'], right['check_id'],
                                              'no_conflict_identified', '适用条件不同', 'tester')
        assert result['ok']
        record = result['data']
        assert api.get_strategy_comparison(record['comparison_id'])['data'] == record
        assert len(api.strategy_comparison_history(right['check_id'])['data']) == 1
        assert not api.save_strategy_comparison(left['check_id'], right['check_id'],
                                                'conflict_identified', '', 'tester')['ok']
        assert not api.get_strategy_comparison('missing')['ok']
    finally:
        api._tasks.close()
