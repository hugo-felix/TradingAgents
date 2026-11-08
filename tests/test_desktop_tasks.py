import json
import sys
import time

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.tasks import TaskManager, validate_request

REQUEST = {'ticker': 'AAPL', 'date': '2026-11-05', 'provider': 'openai',
           'quick_model': 'test', 'deep_model': 'test'}


def test_worker_events_survive_windows_pipe_encoding(monkeypatch):
    import io

    from tradingagents.desktop.worker import emit

    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding='cp936')
    monkeypatch.setattr(sys, 'stdout', stream)
    emit('phase', phase='行情分析已完成')
    line = output.getvalue().decode('utf-8')
    event = json.loads(line.removeprefix('TA_DESKTOP_EVENT:'))
    assert event['phase'] == '行情分析已完成'


def until_done(manager):
    deadline = time.monotonic() + 5
    while manager.status()['status'] in {'running', 'cancelling'} and time.monotonic() < deadline:
        time.sleep(.02)
    return manager.status()


def test_input_validation_rejects_paths_unknown_config_and_future_dates():
    with pytest.raises(ValueError):
        validate_request({**REQUEST, 'ticker': '../../secrets'})
    with pytest.raises(ValueError):
        validate_request({**REQUEST, 'date': '2999-01-01'})
    with pytest.raises(ValueError):
        validate_request({**REQUEST, 'results_dir': '/etc'})
    assert validate_request(REQUEST)['ticker'] == 'AAPL'


def test_one_active_task_and_cancel_reaps_process(tmp_path):
    store = DesktopStore(tmp_path)
    manager = TaskManager(store, lambda _: [sys.executable, '-c', 'import time; time.sleep(30)'])
    assert manager.start(REQUEST)['status'] == 'running'
    with pytest.raises(ValueError):
        manager.start(REQUEST)
    manager.cancel()
    assert until_done(manager)['status'] == 'cancelled'
    assert manager.process.poll() is not None
    assert store.list_reports() == []


def test_worker_failure_is_not_a_completed_report_or_secret_leak(tmp_path):
    manager = TaskManager(DesktopStore(tmp_path), lambda _: [sys.executable, '-c',
        "import sys; print('secret-token', file=sys.stderr); sys.exit(1)"])
    manager.start(REQUEST)
    result = until_done(manager)
    assert result['status'] == 'failed'
    assert result['progress'][-1]['status'] == 'pending'
    assert 'secret-token' not in json.dumps(result)
    diagnostic = (tmp_path / 'runs' / result['id'] / 'diagnostics.jsonl').read_text()
    assert 'secret-token' not in diagnostic
    assert json.loads(diagnostic)['status'] == 'failed'


def test_success_requires_report_and_handles_real_process_output(tmp_path):
    store = DesktopStore(tmp_path)
    script = '''
import json, pathlib, sys
request = pathlib.Path(sys.argv[1])
folder = request.parent
(folder / 'complete_report.md').write_text('# Trading Analysis Report: AAPL\\n- Analysis date: 2026-11-05\\n\\n## V. Portfolio Manager Decision\\n\\n**Rating**: Hold\\n\\n**Executive Summary**: Wait for evidence.')
print('TA_DESKTOP_EVENT:' + json.dumps({'type':'phase', 'phase':'完成分析'}), flush=True)
'''
    manager = TaskManager(store, lambda request: [sys.executable, '-c', script, str(request)])
    manager.start(REQUEST)
    result = until_done(manager)
    assert result['status'] == 'completed'
    assert result['report_id'] == store.list_reports()[0]['id']
    assert result['phase'] == '报告已保存'
    assert result['progress'][-1]['status'] == 'completed'


def test_zero_exit_without_report_is_failure_and_restart_is_interrupted(tmp_path):
    store = DesktopStore(tmp_path)
    manager = TaskManager(store, lambda _: [sys.executable, '-c', 'pass'])
    manager.start(REQUEST)
    assert until_done(manager)['status'] == 'failed'
    store.setting('task', {'status': 'running', 'ticker': 'AAPL'})
    restarted = TaskManager(store)
    assert restarted.status()['status'] == 'interrupted'
    assert restarted.status()['finished_at'] is None


@pytest.mark.parametrize('status', ['running', 'cancelling'])
def test_restart_freezes_duration_at_last_activity(tmp_path, status):
    store = DesktopStore(tmp_path)
    last_activity = '2026-11-05T10:03:00+08:00'
    store.setting('task', {'status': status, 'started_at': '2026-11-05T10:00:00+08:00',
                           'last_activity_at': last_activity})
    restarted = TaskManager(store)
    assert restarted.status()['status'] == 'interrupted'
    assert restarted.status()['finished_at'] == last_activity
    assert TaskManager(store).status()['finished_at'] == last_activity


def test_bridge_errors_have_no_arbitrary_path_or_traceback(tmp_path):
    api = DesktopAPI(DesktopStore(tmp_path))
    result = api.get_report('../../etc/passwd')
    assert result['ok'] is False
    assert 'Traceback' not in result['error']
    assert api.open_external('file:///etc/passwd')['ok'] is False
    assert api.open_external('javascript:alert(1)')['ok'] is False
    assert api.save_settings({'api_key': 'must-not-store'})['ok'] is False
    assert 'must-not-store' not in api._store.database.read_bytes().decode(errors='ignore')


def test_actual_worker_reports_validation_failure_without_traceback(tmp_path):
    import subprocess

    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'ticker': '../../invalid'}))
    result = subprocess.run([sys.executable, '-m', 'tradingagents.desktop.worker', str(request)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert 'TA_DESKTOP_EVENT:' in result.stdout
    assert 'ValueError' in result.stdout
    assert 'Traceback' not in result.stderr


def test_worker_uses_existing_graph_protocol_and_keeps_each_run_separate(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from tradingagents.desktop.worker import run
    from tradingagents.reporting import write_report_tree

    configurations = []

    class Graph:
        def __init__(self, config, callbacks=None):
            configurations.append(config)
            self.config = config
            self.propagator = SimpleNamespace(get_graph_args=lambda **kwargs: {})

        def create_run_state(self, ticker, day):
            return {'company_of_interest': ticker, 'trade_date': day}

        def stream_run(self, initial, **args):
            yield [], {'news_report': 'News evidence'}
            yield [], {'market_report': 'Market evidence'}
            yield [], {'fundamentals_report': 'Fundamentals'}
            yield [], {'sentiment_report': 'Sentiment'}
            # All reports have arrived, but Memory Log still holds the barrier.
            yield [], {'memory_note': 'Memory log settled'}
            yield [], {**initial, 'news_report': 'News evidence', 'market_report': 'Market evidence',
                       'fundamentals_report': 'Fundamentals', 'sentiment_report': 'Sentiment'}
            yield [], {'investment_plan': 'Research'}
            yield [], {'trader_investment_plan': 'Plan'}
            yield [], {**initial, 'final_rating': 'Hold',
                       'final_trade_decision': '**Rating**: Hold\n\n**Executive Summary**: Wait for evidence.'}

        def record_decision(self, *args):
            pass

        def save_reports(self, final, ticker, folder, html):
            return write_report_tree(final, ticker, folder, html=html)

    monkeypatch.setattr('tradingagents.graph.trading_graph.TradingAgentsGraph', Graph)
    folder = tmp_path / 'runs' / 'unique-id'
    folder.mkdir(parents=True)
    request = folder / 'request.json'
    request.write_text(json.dumps(REQUEST))
    run(request)
    events = [json.loads(line.split(':', 1)[1]) for line in capsys.readouterr().out.splitlines()
              if line.startswith('TA_DESKTOP_EVENT:')]
    progress = [event for event in events if 'completed' in event]
    assert progress[0]['completed'] == ['initialize']
    assert len(progress[0]['active']) == 4
    assert progress[1]['completed'] == ['initialize', 'news_report']
    assert 'news_report' not in progress[1]['active']
    assert progress[4]['active'] == []
    assert '等待研究记忆整理' in progress[4]['phase']
    assert len(progress[4]['completed']) == 5
    assert [event['active'] for event in progress[-4:]] == [
        ['investment_plan'], ['trader_investment_plan'], ['final_trade_decision'], ['publish']]
    assert 'publish' not in progress[-1]['completed']
    assert (folder / 'complete_report.md').exists()
    assert json.loads((folder / 'run_metadata.json').read_text())['run_id'] == 'unique-id'
    assert configurations[0]['results_dir'] == str(folder / 'logs')
    assert configurations[0]['quick_think_provider'] is None


def test_progress_is_durable_isolated_and_not_completed_on_cancel(tmp_path):
    script = '''
import json, time
print('TA_DESKTOP_EVENT:[]', flush=True)
print('TA_DESKTOP_EVENT:' + json.dumps({'type': 'phase', 'phase': 'News done',
      'completed': ['initialize', 'news_report', 'unknown', 'publish'],
      'active': ['market_report', 'fundamentals_report', 'sentiment_report']}), flush=True)
time.sleep(30)
'''
    store = DesktopStore(tmp_path)
    manager = TaskManager(store, lambda _: [sys.executable, '-c', script])
    try:
        manager.start(REQUEST)
        deadline = time.monotonic() + 5
        while manager.status()['phase'] != 'News done' and time.monotonic() < deadline:
            time.sleep(.02)
        snapshot = manager.status()
        assert snapshot['phase'] == 'News done'
        assert sum(step['status'] == 'completed' for step in snapshot['progress']) == 2
        assert sum(step['status'] == 'running' for step in snapshot['progress']) == 3
        assert store.setting('task')['progress'] == snapshot['progress']
        assert store.setting('task')['last_activity_at'] == snapshot['last_activity_at']
        snapshot['progress'][0]['status'] = 'pending'
        assert manager.status()['progress'][0]['status'] == 'completed'
        manager.cancel()
        result = until_done(manager)
        assert result['status'] == 'cancelled'
        assert result['progress'][-1]['status'] == 'pending'
        assert store.list_reports() == []
    finally:
        manager.close()


def test_orphan_worker_exits_instead_of_continuing_model_calls():
    import subprocess

    script = ('from tradingagents.desktop.worker import start_parent_guard; '
              'import time; start_parent_guard(0); time.sleep(5)')
    result = subprocess.run([sys.executable, '-c', script], timeout=8)
    assert result.returncode == 2


@pytest.mark.parametrize('idle_timeout,total_timeout,emit_progress', [
    (.3, 5, False), (5, .5, True),
])
def test_stalled_worker_is_reaped_without_publishing(tmp_path, idle_timeout,
                                                     total_timeout, emit_progress):
    script = ("""
import time
while True:
    print('TA_DESKTOP_EVENT:{"type":"phase","phase":"waiting"}', flush=True)
    time.sleep(.05)
""" if emit_progress else "import time; time.sleep(30)")
    store = DesktopStore(tmp_path)
    manager = TaskManager(store, lambda _: [sys.executable, '-c', script],
                          idle_timeout=idle_timeout, total_timeout=total_timeout)
    try:
        manager.start(REQUEST)
        result = until_done(manager)
        assert result['status'] == 'failed'
        assert result['error_kind'] == 'TaskTimeout'
        assert manager.process.poll() is not None
        assert store.list_reports() == []
        assert store.setting('task')['status'] == 'failed'
    finally:
        manager.close()


def test_call_activity_prevents_false_idle_timeout_without_completing_steps(tmp_path):
    script = '''
import time
for _ in range(12):
    print('TA_DESKTOP_EVENT:{"type":"activity","activity":"model_end"}', flush=True)
    time.sleep(.05)
time.sleep(30)
'''
    manager = TaskManager(DesktopStore(tmp_path), lambda _: [sys.executable, '-c', script],
                          idle_timeout=.4, total_timeout=5)
    try:
        task = manager.start(REQUEST)
        time.sleep(.65)
        assert manager.status()['status'] == 'running'
        assert all(step['status'] != 'completed' for step in manager.status()['progress'])
        result = until_done(manager)
        assert result['status'] == 'failed'
        assert result['error_kind'] == 'TaskTimeout'
        rows = (tmp_path / 'runs' / task['id'] / 'diagnostics.jsonl').read_text().splitlines()
        assert json.loads(rows[-1])['error_kind'] == 'TaskTimeout'
    finally:
        manager.close()
