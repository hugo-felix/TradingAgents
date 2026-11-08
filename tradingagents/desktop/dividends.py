"""Cancellable screening jobs; publish only validated, completed snapshots."""
from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import threading
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import uuid4

from tradingagents.dataflows.dividend_screen import (
    UNIVERSE,
    atomic_json,
    calendar_target,
    screen_command,
    write_summary,
)
from tradingagents.desktop.store import now


def snapshot_changes(previous, current):
    """Compare published observations only; absence is not a market conclusion."""
    result = {'previous_at': None, 'items': []}
    if not previous or previous.get('schema_version') != current['schema_version']:
        return result
    if previous.get('discovery', {}).get('scope') != current.get('discovery', {}).get('scope'):
        return result
    if previous.get('core_rules') != current.get('core_rules'):
        return result
    if any(not isinstance(row.get('core'), dict)
           for row in previous['items'] + current['items']):
        return result
    result['previous_at'] = previous['generated_at']
    old = {row['code']: row for row in previous['items']}
    new = {row['code']: row for row in current['items']}
    for code in sorted(old.keys() | new.keys()):
        before, after = old.get(code), new.get(code)
        was_selected = before and before.get('core', {}).get('status') == 'pass'
        selected = after and after.get('core', {}).get('status') == 'pass'
        reasons = []
        kind = 'updated'
        if selected and not was_selected:
            kind = 'entered'
            reasons.append('本次精选门槛全部通过')
        elif was_selected and not selected:
            kind = 'exited'
            if not after:
                reasons.append('未进入本次采集范围，不能据此判断股息或财务恶化')
            else:
                checks = after.get('core', {}).get('checks', []) + after.get('checks', [])
                reasons.extend(dict.fromkeys(
                    f"{'待核验' if c['status'] == 'pending' else '未通过'}：{c['label']}"
                    for c in checks if c.get('required', True) and c['status'] != 'pass'))
                if not reasons:
                    reasons.append('精选依据不足，需重新核验')
        if before and after:
            a, b = before['metrics'], after['metrics']
            # Comparing different dividend definitions would create false alerts.
            if a.get('yield_basis') == b.get('yield_basis') == 'annual_completed_v2':
                x, y = a.get('yield_pct'), b.get('yield_pct')
                if x is not None and y is not None and (x > 4) != (y > 4):
                    reasons.append(f'年度股息率 {x:.2f}% → {y:.2f}%，'
                                   f"{'超过' if y > 4 else '不再超过'}4%")
                x, y = a.get('dividend_annual'), b.get('dividend_annual')
                if (a.get('dividend_year') is not None
                        and a.get('dividend_year') == b.get('dividend_year')
                        and x is not None and y is not None and y < x):
                    reasons.append(f'同年度已实施每股分红下调：{x:g} → {y:g} 元，请复核来源')
        if reasons:
            row = after or before
            result['items'].append({'code': code, 'name': row['name'], 'kind': kind,
                                    'reasons': reasons})
    return result


class DividendJobs:
    def __init__(self, store, command_builder=None):
        self.store = store
        self.command_builder = command_builder or screen_command
        self.lock = threading.RLock()
        self.process = None
        self.thread = None
        self.closed = False
        self.auto_stop = threading.Event()
        self.auto_thread = None
        self.current = store.setting('dividend_job') or {'status': 'idle'}
        if self.current['status'] in {'running', 'cancelling'}:
            self.current.update(status='interrupted', phase='上次刷新已中断，旧清单已保留')
            self._persist()

    def _persist(self):
        self.store.setting('dividend_job', self.current)

    def status(self):
        with self.lock:
            return {**copy.deepcopy(self.current),
                    'auto_update': self.store.setting('dividend_auto_update') is not False}

    def set_auto_update(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('自动更新选项无效。')
        with self.lock:
            self.store.setting('dividend_auto_update', enabled)
        return self.status()

    def start_auto_update(self):
        """Run only while the desktop owns this workspace; no OS scheduled task."""
        with self.lock:
            if self.closed or self.auto_thread:
                return
            self.auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
            self.auto_thread.start()

    def _auto_loop(self):
        while not self.auto_stop.is_set():
            # A transient filesystem/start error must not stop future checks.
            with suppress(Exception):
                self.auto_tick()
            self.auto_stop.wait(60)

    def auto_tick(self, current_time=None):
        """Catch up after 18:00 Shanghai; at most three hourly attempts per date."""
        local = (current_time or datetime.now(UTC)).astimezone(
            timezone(timedelta(hours=8)))
        target = local.date()
        if local.hour < 18:
            target -= timedelta(days=1)
        while target.weekday() > 4:
            target -= timedelta(days=1)
        with self.lock:
            if (self.closed or self.store.setting('dividend_auto_update') is False
                    or self.current['status'] in {'running', 'cancelling'}):
                return
            try:
                snapshot = json.loads((self.store.directory / 'dividend-screen.json').read_text(
                    encoding='utf-8'))
                generated = datetime.fromisoformat(snapshot['generated_at'])
                if generated.tzinfo is None:
                    return
            except (OSError, ValueError, KeyError, TypeError):
                # First acquisition is explicit; never silently launch on an empty workspace.
                return
            session = calendar_target(snapshot.get('trading_calendar'), target)
            if session is not None:
                target = session
            cutoff = datetime.combine(target, datetime.min.time(), tzinfo=local.tzinfo).replace(
                hour=18)
            roster = snapshot.get('discovery', {}).get('roster', {})
            needs_retry = (roster.get('status') != 'confirmed'
                           or bool(roster.get('missing_quote_codes'))
                           or any(row.get('metrics', {}).get('price') is None
                                  or row.get('metrics', {}).get('dividend_annual') is None
                                  for row in snapshot.get('items', [])))
            context = snapshot.get('market_context') or {}
            environment_missing = (context.get('as_of') != snapshot.get('as_of')
                                   or context.get('market', {}).get('status') not in ('normal', 'caution')
                                   or any(context.get('sectors', {}).get(row.get('sector'), {}).get(
                                       'status') not in ('normal', 'caution')
                                       for row in snapshot.get('items', [])
                                       if row.get('status') == 'pass'
                                       and (row.get('core') or {}).get('status') == 'pass'))
            if generated >= cutoff and not needs_retry and not environment_missing:
                return
            if self.current.get('status') == 'cancelled' and str(
                    self.current.get('finished_at', ''))[:10] == local.date().isoformat():
                return
            attempts = self.store.setting('dividend_auto_attempts') or {}
            if attempts.get('date') != local.date().isoformat():
                attempts = {'date': local.date().isoformat(), 'count': 0}
            if attempts['count'] >= 3:
                return
            previous = attempts.get('last_at')
            if previous and (local - datetime.fromisoformat(previous)).total_seconds() < 3600:
                return
            attempts.update(count=attempts['count'] + 1, last_at=local.isoformat())
            self.store.setting('dividend_auto_attempts', attempts)
            try:
                recent_snapshot = 0 <= (local.date() - date.fromisoformat(
                    snapshot['as_of'])).days <= 4
            except (KeyError, TypeError, ValueError):
                recent_snapshot = False
            self.start(direct=self.current.get('direct', False),
                       environment_only=generated >= cutoff and not needs_retry
                       and recent_snapshot)

    def start(self, direct=False, environment_only=False):
        if type(direct) is not bool or type(environment_only) is not bool:
            raise ValueError('直连选项无效。')
        with self.lock:
            if self.closed:
                raise ValueError('应用正在关闭。')
            if self.current['status'] in {'running', 'cancelling'}:
                raise ValueError('已有筛选正在刷新。')
            identifier = uuid4().hex
            folder = self.store.directory / 'dividend-runs' / identifier
            folder.mkdir(parents=True)
            command = [*self.command_builder(), '--data-dir', str(folder),
                       '--cache-dir', str(self.store.directory / 'dividend-cache'),
                       '--official-evidence',
                       str(self.store.directory / 'dividend-official-evidence.json')]
            if direct:
                command.append('--direct')
            if environment_only:
                command.extend(['--environment-snapshot',
                                str(self.store.directory / 'dividend-screen.json')])
            self.process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
                env=dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1'),
                start_new_session=os.name != 'nt',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self.current = {'id': identifier, 'status': 'running', 'completed': 0,
                            'total': 0, 'started_at': now(), 'direct': direct,
                            'environment_only': environment_only,
                            'phase': '正在补充大盘与行业行情' if environment_only else '正在扫描全A股行情'}
            self._persist()
            self.thread = threading.Thread(target=self._monitor,
                                           args=(self.process, folder), daemon=True)
            self.thread.start()
            return self.status()

    def _monitor(self, process, folder):
        from tradingagents.desktop.api import validate_dividend_snapshot
        completed = set()
        try:
            for line in process.stdout:
                discovery = re.fullmatch(r'UNIVERSE (\d+)\s*', line)
                if discovery:
                    with self.lock:
                        self.current.update(total=int(discovery[1]), phase='正在获取候选财报与复权行情')
                        self._persist()
                    continue
                match = re.fullmatch(r'(\d{6}) .+: (pass|pending|fail)\s*', line)
                if not match:
                    continue
                with self.lock:
                    if self.current['status'] != 'running':
                        continue
                    completed.add(match[1])
                    self.current.update(completed=len(completed), phase='正在逐项核验候选')
                    self._persist()
            code = process.wait()
            with self.lock:
                if self.current['status'] == 'cancelling':
                    self.current.update(status='cancelled', phase='刷新已取消，旧清单已保留')
                else:
                    if code != 0:
                        raise ValueError('worker failed')
                    data = json.loads((folder / 'dividend-screen.json').read_text(encoding='utf-8'))
                    validate_dividend_snapshot(data)
                    if data['schema_version'] == 1 and (len(data['items']) != len(UNIVERSE)
                            or {row['code'] for row in data['items']} != {row[0] for row in UNIVERSE}):
                        raise ValueError('incomplete universe')
                    if data['items'] and all(
                            row.get('errors') and not row.get('provenance', {}).get('dividends')
                            for row in data['items']):
                        raise ValueError('provider unavailable for every candidate')
                    # Cancellation and publication share this lock: a cancelled job cannot publish.
                    previous = None
                    path = self.store.directory / 'dividend-screen.json'
                    try:
                        previous = json.loads(path.read_text(encoding='utf-8'))
                        validate_dividend_snapshot(previous)
                    except (OSError, ValueError, TypeError, OverflowError):
                        previous = None
                    if not self.current.get('environment_only'):
                        data['changes'] = snapshot_changes(previous, data)
                    atomic_json(self.store.directory / 'dividend-screen.json', data)
                    self.current.update(status='completed', completed=len(data['items']), total=len(data['items']),
                                        phase=('大盘与行业已更新，缺项请查看面板提示'
                                               if self.current.get('environment_only') else
                                               '筛选已更新，数据缺口请查看逐项依据'))
                    try:
                        write_summary(self.store.directory, data)
                    except OSError:
                        self.current['phase'] = '筛选已更新，但 Markdown 导出失败，请检查目录写入权限'
                self.current['finished_at'] = now()
                self._persist()
        except Exception:
            with self.lock:
                cancelled = self.current['status'] == 'cancelling'
                self.current.update(status='cancelled' if cancelled else 'failed',
                                    phase=('刷新已取消，旧清单已保留' if cancelled else
                                           '刷新失败，旧清单已保留。请检查网络或尝试直连后重试。'),
                                    finished_at=now())
                self._persist()
        finally:
            if process.stdout:
                process.stdout.close()

    def cancel(self):
        with self.lock:
            if self.current['status'] != 'running' or not self.process:
                return self.status()
            self.current.update(status='cancelling', phase='正在停止刷新')
            self._persist()
            process = self.process
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False)
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
        with self.lock:
            self.closed = True
        self.auto_stop.set()
        if self.auto_thread:
            self.auto_thread.join(timeout=2)
        self.cancel()
        if self.thread:
            self.thread.join(timeout=5)
