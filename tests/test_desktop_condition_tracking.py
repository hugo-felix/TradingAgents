import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.condition_tracking import ConditionTrackingRegistry, main
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.strategies import RULE_FIELDS, SCOPE_FIELDS


def setup(directory, *, age=True, metric_right=False):
    registry = ConditionTrackingRegistry(DesktopStore(directory))
    left = {'metric': 'close', **({'max_age_seconds': 86400} if age else {})}
    definition = {**dict.fromkeys(SCOPE_FIELDS, 'synthetic assumption'), 'name': 'synthetic',
                  'market': 'synthetic', 'timeframe': 'daily', 'rules': {
                      key: [{'text': 'manual', 'citation': 'test', 'kind': 'personal',
                             'state': 'manual'}] for key in RULE_FIELDS}}
    definition['rules']['entry'] = [{
        'text': 'test condition', 'citation': 'synthetic', 'kind': 'personal', 'state': 'defined',
        'check': {'op': 'gt', 'left': left,
                  'right': {'metric': 'baseline', 'max_age_seconds': 86400} if metric_right else
                  {'constant': 20, 'unit': 'CNY/share', 'source': 'synthetic'}}}]
    draft = registry.checks.strategies.save(definition)
    strategy = registry.checks.strategies.confirm(draft['strategy_id'], expected_version=1,
                                                 confirmed_by='tester')

    def check(day, value=30, *, observed=None, revision='v1', source='synthetic',
              right_day=None, cutoff=None):
        def series(number, val):
            at = f'2020-01-{number:02}T15:00:00+08:00'
            return {'unit': 'CNY/share', 'observations': [
                {'observed_at': at, 'available_at': at, 'value': val}]}
        data = {'symbol': 'SYNTHETIC', 'market': 'synthetic', 'timeframe': 'daily',
                'price_basis': 'raw', 'source': source, 'revision': revision,
                'series': {'close': series(observed or day, value)}}
        if metric_right:
            data['series']['baseline'] = series(right_day or day, 20)
        return registry.checks.check(strategy['strategy_id'], strategy['version'],
                                     as_of=cutoff or f'2020-01-{day:02}T16:00:00+08:00', data=data)

    seed = check(2)
    plan = registry.create(seed['check_id'], 'entry', 0, 2, '2020-01-20T16:00:00+08:00', 'tester')
    return registry, plan, check


def evaluate(registry, plan, check):
    return registry.evaluate(plan['plan_id'], check['check_id'])


def test_distinct_observations_debounce_rearm_and_immutable_history(tmp_path):
    registry, plan, check = setup(tmp_path)
    first = evaluate(registry, plan, check(2))
    assert first['result']['consecutive'] == 1
    assert not first['result']['hit']
    second = evaluate(registry, plan, check(3))
    assert second['result']['status'] == 'triggered'
    assert second['result']['hit']
    assert second['previous_event_id'] == first['event_id']
    assert evaluate(registry, plan, check(4))['result']['status'] == 'sustained'
    assert evaluate(registry, plan, check(5, 10))['result']['consecutive'] == 0
    assert not evaluate(registry, plan, check(6))['result']['hit']
    assert evaluate(registry, plan, check(7))['result']['hit']
    original = copy.deepcopy(registry.history(plan['plan_id']))
    first['check']['data']['symbol'] = 'changed'
    second['result']['hit'] = False
    reopened = ConditionTrackingRegistry(DesktopStore(tmp_path))
    assert reopened.history(plan['plan_id']) == original
    assert reopened.list() == [plan]
    assert reopened.get(plan['plan_id']) == plan
    assert evaluate(reopened, plan, check(2)) == original[-1]
    assert registry.checks.strategies.get(plan['strategy_id'], plan['version'])['status'] == 'confirmed'


def test_repeated_checks_revisions_and_timezone_do_not_count_as_new_observations(tmp_path):
    registry, plan, check = setup(tmp_path)
    evaluate(registry, plan, check(2))
    for day, revision in [(3, 'v1'), (4, 'v2')]:
        # Still within the max age at these explicitly supplied cutoffs.
        result = evaluate(registry, plan, check(day, observed=2, revision=revision,
                          cutoff=f'2020-01-02T{16 + day:02}:00:00+08:00'))['result']
        assert result['status'] == 'waiting_for_observation'
        assert result['consecutive'] == 1
        assert not result['hit']
    assert evaluate(registry, plan, check(3))['result']['hit']


@pytest.mark.parametrize('failure', ['missing', 'stale', 'unconfigured', 'regressed'])
def test_unknown_or_unusable_data_resets_and_old_observations_cannot_resume(tmp_path, failure):
    registry, plan, check = setup(tmp_path, age=failure != 'unconfigured')
    first = evaluate(registry, plan, check(2))
    if failure == 'unconfigured':
        assert first['result']['reason'] == 'freshness_not_evaluated'
        assert not first['result']['hit']
        return
    candidate = (check(3, None) if failure == 'missing' else
                 check(4, observed=2) if failure == 'stale' else
                 check(3, observed=1, cutoff='2020-01-02T17:00:00+08:00'))
    receipt = evaluate(registry, plan, candidate)
    assert receipt['result']['status'] == 'paused'
    assert receipt['result']['consecutive'] == 0
    resumed = evaluate(registry, plan, check(5))
    assert resumed['result']['consecutive'] == 1
    assert not resumed['result']['hit']


def test_both_metric_operands_must_advance_and_revision_failure_interrupts(tmp_path):
    registry, plan, check = setup(tmp_path, metric_right=True)
    evaluate(registry, plan, check(2))
    held = evaluate(registry, plan, check(3, right_day=2,
                    cutoff='2020-01-03T15:00:00+08:00'))
    assert held['result']['status'] == 'waiting_for_observation'
    assert not held['result']['hit']
    failed = evaluate(registry, plan, check(3, 10, revision='v2'))
    assert failed['result']['status'] == 'not_met'
    corrected = evaluate(registry, plan, check(3, revision='v3',
                         cutoff='2020-01-03T17:00:00+08:00'))
    assert corrected['result']['consecutive'] == 0
    assert not corrected['result']['hit']
    assert evaluate(registry, plan, check(4))['result']['consecutive'] == 1


def test_exact_expiry_and_out_of_order_have_no_hits_or_partial_writes(tmp_path):
    registry, plan, check = setup(tmp_path)
    evaluate(registry, plan, check(3))
    for candidate in [check(2), check(3, revision='other'),
                      check(1)]:
        with pytest.raises(ValueError):
            evaluate(registry, plan, candidate)
    assert len(registry.history(plan['plan_id'])) == 1
    expired = evaluate(registry, plan, check(20))
    assert expired['result']['status'] == 'expired'
    assert expired['result']['consecutive'] == 0
    assert not expired['result']['hit']
    assert evaluate(registry, plan, check(21))['result']['status'] == 'expired'


@pytest.mark.parametrize('change', ['source', 'symbol', 'price_basis', 'unit', 'version'])
def test_context_and_exact_strategy_version_are_pinned(tmp_path, change):
    registry, plan, check = setup(tmp_path)
    base = check(3)
    data = base['data']
    version = 2
    if change == 'version':
        strategy = registry.checks.strategies.get(plan['strategy_id'], 2)
        registry.checks.strategies.save(strategy['definition'], strategy_id=plan['strategy_id'],
                                        expected_version=2)
        registry.checks.strategies.confirm(plan['strategy_id'], expected_version=3,
                                          confirmed_by='tester')
        version = 4
    elif change == 'unit':
        data['series']['close']['unit'] = 'USD/share'
    else:
        data[change] = 'other'
    candidate = registry.checks.check(plan['strategy_id'], version, as_of=base['as_of'], data=data)
    with pytest.raises(ValueError):
        evaluate(registry, plan, candidate)
    assert registry.history(plan['plan_id']) == []


@pytest.mark.parametrize('args', [
    ('entry', True, 2, '2020-01-20T16:00:00+08:00', 'tester'),
    ('entry', 0, True, '2020-01-20T16:00:00+08:00', 'tester'),
    ('entry', 0, 0, '2020-01-20T16:00:00+08:00', 'tester'),
    ('entry', 0, 101, '2020-01-20T16:00:00+08:00', 'tester'),
    ('missing', 0, 2, '2020-01-20T16:00:00+08:00', 'tester'),
    ('entry', 0, 2, '2020-01-02T08:00:00Z', 'tester'),
    ('entry', 0, 2, '2020-01-20', 'tester'),
    ('entry', 0, 2, '2020-01-20T16:00:00+08:00', ' '),
])
def test_plan_validation_does_not_create_records(tmp_path, args):
    registry, plan, check = setup(tmp_path)
    with pytest.raises(ValueError):
        registry.create(check(2)['check_id'], *args)
    assert registry.list() == [plan]


def test_concurrent_retry_atomic_append_and_bridge(tmp_path):
    registry, plan, check = setup(tmp_path)
    seed = check(2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: evaluate(registry, plan, seed), range(8)))
    assert all(row == receipts[0] for row in receipts)
    assert len(registry.history(plan['plan_id'])) == 1
    api = DesktopAPI(registry.store)
    try:
        assert api.create_condition_tracking(seed['check_id'], 'entry', 0, 2,
                                            plan['expires_at'], 'tester')['data'] == plan
        assert api.list_condition_tracking()['data'] == [plan]
        assert api.get_condition_tracking(plan['plan_id'])['data'] == plan
        assert api.evaluate_condition_tracking(plan['plan_id'], seed['check_id'])['data'] == receipts[0]
        assert api.condition_tracking_history(plan['plan_id'])['data'] == receipts[:1]
        assert not api.evaluate_condition_tracking(plan['plan_id'], 'missing')['ok']
        assert not api.get_condition_tracking('missing')['ok']
        assert not api.condition_tracking_history('missing')['ok']
    finally:
        api._tasks.close()


def test_cli_and_backup_restore(tmp_path, capsys):
    from tradingagents.desktop.backup import create_backup, restore_backup

    workspace = tmp_path / 'workspace'
    registry, plan, check = setup(workspace)
    prefix = ['--workspace', str(workspace)]
    main([*prefix, 'create', check(2)['check_id'], 'entry', '0', '--required-consecutive', '2',
          '--expires-at', plan['expires_at'], '--created-by', 'tester'])
    assert json.loads(capsys.readouterr().out) == plan
    main([*prefix, 'evaluate', plan['plan_id'], check(2)['check_id']])
    first = json.loads(capsys.readouterr().out)
    for action, expected in [('get', plan), ('history', [first])]:
        main([*prefix, action, plan['plan_id']])
        assert json.loads(capsys.readouterr().out) == expected
    main([*prefix, 'list'])
    assert json.loads(capsys.readouterr().out) == [plan]
    next_check = check(3)
    archive = tmp_path / 'backup.zip'
    create_backup(workspace, archive)
    restored = tmp_path / 'restored'
    restore_backup(archive, restored)
    reopened = ConditionTrackingRegistry(DesktopStore(restored))
    assert reopened.history(plan['plan_id']) == [first]
    assert evaluate(reopened, plan, next_check)['result']['hit']


def test_failed_append_rolls_back_and_retry_advances_once(tmp_path):
    registry, plan, check = setup(tmp_path)
    first = evaluate(registry, plan, check(2))
    candidate = check(3)
    with registry.store.connect() as db:
        db.execute("CREATE TRIGGER fail_tracking BEFORE INSERT ON condition_tracking_events "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        evaluate(registry, plan, candidate)
    assert registry.history(plan['plan_id']) == [first]
    with registry.store.connect() as db:
        db.execute('DROP TRIGGER fail_tracking')
    assert evaluate(registry, plan, candidate)['result']['hit']
    assert len(registry.history(plan['plan_id'])) == 2


def test_manual_and_constant_only_rules_cannot_trigger(tmp_path):
    registry, plan, check = setup(tmp_path)
    seed = check(2)
    manual = registry.create(seed['check_id'], 'exit', 0, 1, plan['expires_at'], 'tester')
    assert evaluate(registry, manual, seed)['result']['reason'] == 'manual'
    strategy = registry.checks.strategies.get(plan['strategy_id'], plan['version'])
    definition = strategy['definition']
    definition['rules']['entry'][0]['check']['left'] = {
        'constant': 30, 'unit': 'CNY/share', 'source': 'test'}
    registry.checks.strategies.save(definition, strategy_id=plan['strategy_id'], expected_version=2)
    registry.checks.strategies.confirm(plan['strategy_id'], expected_version=3, confirmed_by='tester')
    constant = registry.checks.check(plan['strategy_id'], 4, as_of=seed['as_of'], data=seed['data'])
    other = registry.create(constant['check_id'], 'entry', 0, 1, plan['expires_at'], 'tester')
    result = evaluate(registry, other, constant)['result']
    assert result['reason'] == 'no_observation'
    assert not result['hit']


def test_pause_does_not_let_old_data_restart_count(tmp_path):
    registry, plan, check = setup(tmp_path)
    evaluate(registry, plan, check(2))
    evaluate(registry, plan, check(2, None, revision='v2',
             cutoff='2020-01-02T17:00:00+08:00'))
    resumed = evaluate(registry, plan, check(2, revision='v3',
                       cutoff='2020-01-02T18:00:00+08:00'))
    assert resumed['result']['status'] == 'waiting_for_observation'
    assert resumed['result']['consecutive'] == 0
    assert not resumed['result']['hit']
    assert evaluate(registry, plan, check(3))['result']['consecutive'] == 1


@pytest.mark.parametrize('state', ['missing', 'incomplete', 'locked'])
def test_cli_refuses_invalid_or_active_workspace(tmp_path, capsys, state):
    from tradingagents.desktop.backup import INCOMPLETE
    from tradingagents.desktop.instance import WorkspaceLock

    if state != 'missing':
        DesktopStore(tmp_path)
    if state == 'incomplete':
        (tmp_path / INCOMPLETE).touch()
    with WorkspaceLock(tmp_path) if state == 'locked' else nullcontext():
        with pytest.raises(SystemExit) as exc:
            main(['--workspace', str(tmp_path), 'list'])
        assert exc.value.code == 1
        assert '操作失败' in capsys.readouterr().err


def test_stop_preserves_plan_and_history_blocks_new_evaluations_and_survives_restore(tmp_path):
    from tradingagents.desktop.backup import create_backup, restore_backup

    workspace = tmp_path / 'workspace'
    registry, plan, check = setup(workspace)
    first = evaluate(registry, plan, check(2))
    hit = evaluate(registry, plan, check(3))
    assert hit['result']['hit']
    candidate = check(4)
    stopped = registry.cancel(plan['plan_id'], ' tester ', ' replaced by another rule ')
    assert stopped['last_event_id'] == hit['event_id']
    assert stopped['cancelled_by'] == 'tester'
    assert stopped['reason'] == 'replaced by another rule'
    assert registry.cancel(plan['plan_id'], 'tester', stopped['reason']) == stopped
    with pytest.raises(ValueError, match='覆盖'):
        registry.cancel(plan['plan_id'], 'other', stopped['reason'])
    with pytest.raises(ValueError, match='停止'):
        evaluate(registry, plan, candidate)
    # Retrying an old hit returns its immutable evidence, never a new hit event.
    assert evaluate(registry, plan, check(3)) == hit
    assert registry.history(plan['plan_id']) == [hit, first]
    expected = dict(plan, cancellation=stopped)
    assert registry.get(plan['plan_id']) == expected
    assert registry.list() == [expected]
    assert registry.create(check(2)['check_id'], 'entry', 0, 2, plan['expires_at'], 'tester') == expected
    with registry.store.connect() as db:
        raw = db.execute('SELECT document FROM condition_tracking_plans').fetchone()[0]
    assert json.loads(raw) == plan
    archive = tmp_path / 'backup.zip'
    create_backup(workspace, archive)
    restored = tmp_path / 'restored'
    restore_backup(archive, restored)
    reopened = ConditionTrackingRegistry(DesktopStore(restored))
    assert reopened.get(plan['plan_id']) == expected
    assert reopened.history(plan['plan_id']) == [hit, first]
    with pytest.raises(ValueError, match='停止'):
        evaluate(reopened, plan, candidate)


@pytest.mark.parametrize('actor,reason', [('', 'reason'), (' ', 'reason'),
                         ('a' * 201, 'reason'), (None, 'reason'), ('tester', ''),
                         ('tester', ' '), ('tester', 'a' * 2001), ('tester', None)])
def test_stop_validation_has_no_partial_writes(tmp_path, actor, reason):
    registry, plan, check = setup(tmp_path)
    with pytest.raises(ValueError):
        registry.cancel(plan['plan_id'], actor, reason)
    assert registry.get(plan['plan_id']) == plan
    assert evaluate(registry, plan, check(2))['result']['consecutive'] == 1


def test_stop_before_first_check_bridge_and_cli(tmp_path, capsys):
    registry, plan, check = setup(tmp_path)
    api = DesktopAPI(registry.store)
    try:
        assert not api.cancel_condition_tracking('missing', 'tester', 'reason')['ok']
        assert not api.cancel_condition_tracking(plan['plan_id'], '', 'reason')['ok']
        main(['--workspace', str(tmp_path), 'cancel', plan['plan_id'],
              '--cancelled-by', 'tester', '--reason', 'reason'])
        stopped = json.loads(capsys.readouterr().out)
        assert stopped['last_event_id'] is None
        assert api.cancel_condition_tracking(plan['plan_id'], 'tester', 'reason')['data'] == stopped
        assert api.get_condition_tracking(plan['plan_id'])['data']['cancellation'] == stopped
        assert api.list_condition_tracking()['data'][0]['cancellation'] == stopped
        assert not api.evaluate_condition_tracking(plan['plan_id'], check(2)['check_id'])['ok']
        assert api.condition_tracking_history(plan['plan_id'])['data'] == []
    finally:
        api._tasks.close()


def test_cancel_and_evaluate_serialize_without_late_writes(tmp_path):
    registry, plan, check = setup(tmp_path)
    evaluate(registry, plan, check(2))
    candidate = check(3)
    barrier = Barrier(8)

    def attempt(_):
        barrier.wait()
        try:
            return evaluate(registry, plan, candidate)
        except ValueError as exc:
            assert '停止' in str(exc)
            return None

    def stop():
        barrier.wait()
        return registry.cancel(plan['plan_id'], 'tester', 'reason')

    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(attempt, n) for n in range(4)]
        stops = [pool.submit(stop) for _ in range(4)]
        results = [future.result() for future in pending]
        cancellations = [future.result() for future in stops]
    assert all(record == cancellations[0] for record in cancellations)
    history = registry.history(plan['plan_id'])
    assert cancellations[0]['last_event_id'] == history[0]['event_id']
    assert len(history) == (2 if any(results) else 1)
    assert all(result is None or result == history[0] for result in results)
    with pytest.raises(ValueError, match='停止'):
        evaluate(registry, plan, check(4))


def test_failed_cancellation_rolls_back_and_allows_retry(tmp_path):
    registry, plan, check = setup(tmp_path)
    with registry.store.connect() as db:
        db.execute('CREATE TRIGGER fail_cancel BEFORE INSERT ON condition_tracking_cancellations '
                   "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        registry.cancel(plan['plan_id'], 'tester', 'reason')
    assert registry.get(plan['plan_id']) == plan
    first = evaluate(registry, plan, check(2))
    with registry.store.connect() as db:
        db.execute('DROP TRIGGER fail_cancel')
    assert registry.cancel(plan['plan_id'], 'tester', 'reason')['last_event_id'] == first['event_id']
