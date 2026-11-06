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
    print(EVENT_PREFIX + json.dumps({'type': event_type, **values}, ensure_ascii=False), flush=True)


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


def run(request_path: Path):
    request = validate_request(json.loads(request_path.read_text(encoding='utf-8')))
    folder = request_path.resolve().parent
    from tradingagents.default_config import build_default_config
    from tradingagents.graph.trading_graph import TradingAgentsGraph

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
    graph = TradingAgentsGraph(config=config)
    ticker, day = request['ticker'], request['date']
    emit('phase', phase='分析引擎已就绪，正在获取数据')
    initial = graph.create_run_state(ticker, day)
    final = {}
    phases = (
        ('market_report', '行情分析已完成'), ('fundamentals_report', '基本面分析已完成'),
        ('news_report', '新闻分析已完成'), ('sentiment_report', '情绪分析已完成'),
        ('investment_plan', '研究结论已完成'), ('trader_investment_plan', '交易计划已完成'),
        ('final_trade_decision', '最终决策已完成，正在保存报告'),
    )
    seen = set()
    for _messages, state in graph.stream_run(initial, **graph.propagator.get_graph_args()):
        if not state:
            continue
        final.update(state)
        for key, title in phases:
            if state.get(key) and key not in seen:
                seen.add(key)
                emit('phase', phase=title)
    if not final.get('final_trade_decision'):
        raise ValueError('Analysis did not produce a final decision')
    graph.record_decision(ticker, day, final)
    graph.save_reports(final, ticker, folder, html=True)
    (folder / 'run_metadata.json').write_text(json.dumps({
        'ticker': ticker, 'analysis_date': day, 'provider': request['provider'], 'run_id': folder.name,
        'model': request['deep_model'],
    }, ensure_ascii=False), encoding='utf-8')
    emit('phase', phase='报告文件已生成')


def main():
    try:
        if parent := os.environ.get('TRADINGAGENTS_DESKTOP_PARENT_PID'):
            start_parent_guard(int(parent))
        if sys.argv[1] == '--check-runtime':
            from tradingagents.graph.trading_graph import TradingAgentsGraph
            assert TradingAgentsGraph
            emit('phase', phase='runtime-ready')
            return
        run(Path(sys.argv[1]))
    except Exception as exc:
        emit('error', kind=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
