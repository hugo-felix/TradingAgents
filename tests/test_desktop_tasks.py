import json
import sys
import time

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.store import DesktopStore
from tradingagents.desktop.tasks import TaskManager, validate_request

REQUEST = {'ticker': 'AAPL', 'date': '2026-11-05', 'provider': 'openai',
           'quick_model': 'test', 'deep_model': 'test'}


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
    assert 'secret-token' not in json.dumps(result)


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


def test_zero_exit_without_report_is_failure_and_restart_is_interrupted(tmp_path):
    store = DesktopStore(tmp_path)
    manager = TaskManager(store, lambda _: [sys.executable, '-c', 'pass'])
    manager.start(REQUEST)
    assert until_done(manager)['status'] == 'failed'
    store.setting('task', {'status': 'running', 'ticker': 'AAPL'})
    restarted = TaskManager(store)
    assert restarted.status()['status'] == 'interrupted'


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


def test_worker_uses_existing_graph_protocol_and_keeps_each_run_separate(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tradingagents.desktop.worker import run
    from tradingagents.reporting import write_report_tree

    configurations = []

    class Graph:
        def __init__(self, config):
            configurations.append(config)
            self.config = config
            self.propagator = SimpleNamespace(get_graph_args=lambda: {})

        def create_run_state(self, ticker, day):
            return {'company_of_interest': ticker, 'trade_date': day}

        def stream_run(self, initial, **args):
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
    assert (folder / 'complete_report.md').exists()
    assert json.loads((folder / 'run_metadata.json').read_text())['run_id'] == 'unique-id'
    assert configurations[0]['results_dir'] == str(folder / 'logs')
    assert configurations[0]['quick_think_provider'] is None


def test_orphan_worker_exits_instead_of_continuing_model_calls():
    import subprocess

    script = ('from tradingagents.desktop.worker import start_parent_guard; '
              'import time; start_parent_guard(0); time.sleep(5)')
    result = subprocess.run([sys.executable, '-c', script], timeout=8)
    assert result.returncode == 2
