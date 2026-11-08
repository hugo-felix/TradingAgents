"""Analysis side process. Stdout carries only prefixed lifecycle events to the UI."""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
from pathlib import Path

from tradingagents.desktop.tasks import EVENT_PREFIX, validate_request


def emit(event_type, **values):
    # ASCII JSON survives Windows pipe encodings while retaining Chinese phase labels.
    print(EVENT_PREFIX + json.dumps({'type': event_type, **values}), flush=True)


def start_parent_guard(parent_pid: int):
    """Stop abandoned model calls if the desktop process crashes."""
    def watch():
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, parent_pid)
            if not handle:
                os._exit(2)
            try:
                if kernel.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:
                    os._exit(2)
            finally:
                kernel.CloseHandle(handle)
        else:
            while os.getppid() == parent_pid:
                time.sleep(1)
            os._exit(2)
    threading.Thread(target=watch, daemon=True).start()


def run(request_path: Path, diagnostics=None):
    request = validate_request(json.loads(request_path.read_text(encoding='utf-8')))
    folder = request_path.resolve().parent
    from tradingagents.default_config import build_default_config
    from tradingagents.desktop.diagnostics import RunDiagnostics
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    diagnostics = diagnostics or RunDiagnostics(folder / 'diagnostics.jsonl', emit)
    diagnostics.record('worker_start')

    config = copy.deepcopy(build_default_config())
    config.update({
        'llm_provider': request['provider'], 'quick_think_llm': request['quick_model'],
        'deep_think_llm': request['deep_model'], 'quick_think_provider': None,
        'deep_think_provider': None, 'backend_url': request.get('backend_url') or None,
        'quick_think_backend_url': None, 'deep_think_backend_url': None,
        'output_language': '简体中文', 'results_dir': str(folder / 'logs'),
        'data_cache_dir': str(folder.parent.parent / 'cache'),
        'memory_log_path': str(folder.parent.parent / 'memory' / 'trading_memory.md'),
        'checkpoint_enabled': False,
    })
    graph = TradingAgentsGraph(config=config, callbacks=[diagnostics])
    ticker, day = request['ticker'], request['date']
    analysts = ['market_report', 'fundamentals_report', 'news_report', 'sentiment_report']
    initial = graph.create_run_state(ticker, day)
    emit('phase', phase='正在并行获取数据与分析：行情、基本面、新闻、情绪',
         completed=['initialize'], active=analysts)
    final = {}
    phases = (
        ('market_report', '行情分析已完成'), ('fundamentals_report', '基本面分析已完成'),
        ('news_report', '新闻分析已完成'), ('sentiment_report', '情绪分析已完成'),
        ('investment_plan', '研究结论已完成'), ('trader_investment_plan', '交易计划已完成'),
        ('final_trade_decision', '最终决策已完成，正在保存报告'),
    )
    seen = {'initialize'}
    research_ready = False
    debate_counts = {}
    for _messages, state in graph.stream_run(
        initial, **graph.propagator.get_graph_args(callbacks=[diagnostics])
    ):
        if not state:
            continue
        final.update(state)
        for key in ('investment_debate_state', 'risk_debate_state'):
            count = (state.get(key) or {}).get('count', 0)
            if count > debate_counts.get(key, 0):
                debate_counts[key] = count
                diagnostics.record('debate_step_end', stage=key, step=count)
        # Individual analyst reports precede the top-level state. Only that state
        # confirms the parallel barrier (including Memory Log) has completed.
        ready = research_ready or all(state.get(key) for key in analysts)
        newly_done = [(key, title) for key, title in phases if state.get(key) and key not in seen]
        if not newly_done and ready == research_ready:
            continue
        research_ready = ready
        seen.update(key for key, _title in newly_done)
        remaining = [item for item in analysts if item not in seen]
        if remaining:
            active = remaining
            phase = f'{newly_done[-1][1]}，其余分析仍在进行'
        elif not research_ready:
            active = []
            phase = '四项分析已完成，正在等待研究记忆整理'
        else:
            next_step = next((item for item, _ in phases if item not in seen), 'publish')
            active = [next_step]
            phase = {
                'investment_plan': '正在进行多空研究与综合结论',
                'trader_investment_plan': '正在制定交易计划',
                'final_trade_decision': '正在进行风险评审与最终决策',
                'publish': '最终决策已完成，正在保存报告',
            }[next_step]
        emit('phase', phase=phase, completed=sorted(seen), active=active)
        diagnostics.record('phase', phase=phase)
    if not final.get('final_trade_decision'):
        raise ValueError('Analysis did not produce a final decision')
    graph.record_decision(ticker, day, final)
    graph.save_reports(final, ticker, folder, html=True)
    (folder / 'run_metadata.json').write_text(json.dumps({
        'ticker': ticker, 'analysis_date': day, 'provider': request['provider'], 'run_id': folder.name,
        'model': request['deep_model'],
    }, ensure_ascii=False), encoding='utf-8')
    emit('phase', phase='报告文件已生成')
    diagnostics.record('worker_complete')


def main():
    diagnostics = None
    try:
        if parent := os.environ.get('TRADINGAGENTS_DESKTOP_PARENT_PID'):
            start_parent_guard(int(parent))
        if sys.argv[1] == '--check-runtime':
            from importlib.resources import files

            from tradingagents.graph.trading_graph import TradingAgentsGraph
            assert TradingAgentsGraph
            assets = files('tradingagents.desktop') / 'static'
            for name in ('index.html', 'app.js', 'app.css', 'stockseek-mark.png'):
                if not (assets / name).is_file():
                    raise FileNotFoundError(f'Missing desktop asset: {name}')
            emit('phase', phase='runtime-ready')
            return
        from tradingagents.desktop.diagnostics import RunDiagnostics

        request_path = Path(sys.argv[1])
        diagnostics = RunDiagnostics(request_path.resolve().parent / 'diagnostics.jsonl', emit)
        run(request_path, diagnostics)
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.failure(exc)
        emit('error', kind=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
