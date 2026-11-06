"""One background analysis process, with durable and truthful lifecycle state."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path
from time import time
from urllib.parse import urlsplit
from uuid import uuid4

from tradingagents.desktop.store import DesktopStore, now, valid_ticker
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

EVENT_PREFIX = 'TA_DESKTOP_EVENT:'
REQUEST_KEYS = {'ticker', 'date', 'provider', 'quick_model', 'deep_model', 'backend_url'}


def validate_request(request: dict) -> dict:
    if not isinstance(request, dict) or set(request) - REQUEST_KEYS:
        raise ValueError('分析配置包含不支持的字段。')
    result = {key: str(value).strip() for key, value in request.items()}
    result['ticker'] = valid_ticker(result.get('ticker', ''))
    try:
        day = date.fromisoformat(result.get('date', ''))
    except ValueError as exc:
        raise ValueError('请选择有效的分析日期。') from exc
    if day > date.today() or day.isoformat() != result['date']:
        raise ValueError('分析日期不能晚于今天。')
    if result.get('provider') not in PROVIDER_API_KEY_ENV:
        raise ValueError('请选择支持的模型服务商。')
    for key in ('quick_model', 'deep_model'):
        if not result.get(key) or len(result[key]) > 160:
            raise ValueError('请填写快速模型和深度模型的 ID。')
    endpoint = result.get('backend_url', '')
    if endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('服务地址必须为不含凭据和查询参数的 HTTP(S) 地址。')
    return result


def worker_command(request_path: Path) -> list[str]:
    if getattr(sys, 'frozen', False):
        executable = str(Path(sys.executable).with_name('StockSeekWorker.exe')) if os.name == 'nt' else sys.executable
        return [executable, '--desktop-worker', str(request_path)]
    return [sys.executable, '-m', 'tradingagents.desktop.worker', str(request_path)]


class TaskManager:
    def __init__(self, store: DesktopStore, command_builder=None, environment_builder=None):
        self.store = store
        self.command_builder = command_builder or worker_command
        self.environment_builder = environment_builder
        self.lock = threading.RLock()
        self.process = None
        self.thread = None
        self.current = store.setting('task') or {'status': 'idle'}
        if self.current['status'] in {'running', 'cancelling'}:
            self.current.update(status='interrupted', phase='上次运行已中断，可重新分析')
            self._persist()

    def _persist(self):
        self.store.setting('task', self.current)

    def status(self):
        with self.lock:
            return {**self.current}

    def start(self, request: dict):
        request = validate_request(request)
        with self.lock:
            if self.current['status'] in {'running', 'cancelling'}:
                raise ValueError('已有分析正在运行，请等待完成或先取消。')
            run_id = uuid4().hex
            folder = self.store.directory / 'runs' / run_id
            folder.mkdir(parents=True)
            request_path = folder / 'request.json'
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
            environment = os.environ.copy()
            environment['PYTHONUNBUFFERED'] = '1'
            environment['TRADINGAGENTS_DESKTOP_PARENT_PID'] = str(os.getpid())
            if self.environment_builder:
                environment.update(self.environment_builder(request['provider']))
            self.process = subprocess.Popen(
                self.command_builder(request_path), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                env=environment, start_new_session=os.name != 'nt',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            self.current = {
                'id': run_id, 'status': 'running', 'ticker': request['ticker'], 'date': request['date'],
                'phase': '正在初始化分析引擎', 'started_at': now(), 'started_epoch': time(),
                'request': request,
            }
            self._persist()
            self.thread = threading.Thread(target=self._monitor, args=(self.process, folder), daemon=True)
            self.thread.start()
            return self.status()

    def _monitor(self, process, folder):
        error_kind = ''
        try:
            for line in process.stdout:
                if not line.startswith(EVENT_PREFIX):
                    continue
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                except ValueError:
                    continue
                with self.lock:
                    if self.current['status'] != 'running':
                        continue
                    if event.get('type') == 'phase':
                        self.current['phase'] = str(event.get('phase', '分析中'))[:100]
                    elif event.get('type') == 'error':
                        # Only worker-controlled error class names; no prompts or API responses.
                        error_kind = str(event.get('kind', '')) if str(event.get('kind', '')).isidentifier() else ''
            code = process.wait()
            with self.lock:
                if self.current['status'] == 'cancelling':
                    self.current.update(status='cancelled', phase='分析已取消，未发布新报告')
                elif code != 0:
                    self.current.update(status='failed', phase='分析失败，请检查模型配置、凭据及数据源后重试', error_kind=error_kind)
                elif not (folder / 'complete_report.md').is_file():
                    self.current.update(status='failed', phase='分析未生成完整报告，请重新运行')
                else:
                    imported = self.store.import_directory(folder)
                    if imported['errors'] or not imported['added']:
                        self.current.update(status='failed', phase='报告读取失败，未发布新结果')
                    else:
                        report_id = imported['ids'][0]
                        self.current.update(status='completed', phase='报告已保存', report_id=report_id)
                self.current['finished_at'] = now()
                self._persist()
        except Exception:
            with self.lock:
                self.current.update(status='failed', phase='后台任务异常结束，请重试', finished_at=now())
                self._persist()
        finally:
            if process.stdout:
                process.stdout.close()

    def cancel(self):
        with self.lock:
            if self.current['status'] != 'running' or not self.process:
                return self.status()
            self.current.update(status='cancelling', phase='正在停止分析')
            self._persist()
            process = self.process
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        except ProcessLookupError:
            pass
        return self.status()

    def close(self):
        self.cancel()
        if self.thread:
            self.thread.join(timeout=5)
