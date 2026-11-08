"""Content-free, flushed diagnostics for a single desktop research run."""

from __future__ import annotations

import json
import threading
import traceback
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic

from langchain_core.callbacks import BaseCallbackHandler


class RunDiagnostics(BaseCallbackHandler):
    def __init__(self, path, notify):
        self.path = path
        self.notify = notify
        self.lock = threading.RLock()
        self.calls = {}

    def record(self, event, **fields):
        with self.lock:
            entry = {'time': datetime.now(UTC).isoformat(), 'event': event, **fields}
            # A full disk must not change a research result or suppress its error event.
            with suppress(OSError), self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(entry, ensure_ascii=True) + '\n')
            self.notify('activity', activity=event)

    def _start(self, kind, run_id, metadata=None):
        with self.lock:
            # Node names are graph-owned. Never serialize prompts, tool arguments,
            # invocation parameters, response bodies, or exception messages.
            node = str((metadata or {}).get('langgraph_node', ''))[:100]
            self.calls[str(run_id)] = (monotonic(), node, kind)
            self.record(kind + '_start', call_id=str(run_id), node=node)

    def _finish(self, run_id, error=None, **metadata):
        with self.lock:
            started, node, kind = self.calls.pop(str(run_id), (monotonic(), '', 'call'))
            fields = {'call_id': str(run_id), 'node': node,
                      'elapsed_seconds': round(monotonic() - started, 3)}
            if error is not None:
                fields['error_kind'] = type(error).__name__
            fields.update(metadata)
            self.record(kind + ('_error' if error is not None else '_end'), **fields)

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        self._start('model', run_id, metadata)

    def on_llm_start(self, serialized, prompts, *, run_id, metadata=None, **kwargs):
        self._start('model', run_id, metadata)

    def on_llm_end(self, response, *, run_id, **kwargs):
        summaries = []
        for batch in getattr(response, 'generations', []):
            for generation in batch:
                message = getattr(generation, 'message', None)
                content = getattr(message, 'content', '')
                info = getattr(generation, 'generation_info', None) or {}
                reason = info.get('finish_reason')
                # Only fixed categories and counts; never serialize provider text.
                summaries.append({
                    'content_chars': len(content) if isinstance(content, str) else None,
                    'tool_calls': len(getattr(message, 'tool_calls', [])),
                    'finish_reason': reason if reason in (
                        'stop', 'length', 'tool_calls', 'content_filter', 'function_call'
                    ) else 'unknown',
                })
                usage = getattr(message, 'usage_metadata', None) or {}
                summaries[-1]['tokens'] = {
                    key: usage[key] for key in ('input_tokens', 'output_tokens', 'total_tokens')
                    if type(usage.get(key)) is int and usage[key] >= 0
                }
        self._finish(run_id, responses=summaries)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error)

    def on_tool_start(self, serialized, input_str, *, run_id, metadata=None, **kwargs):
        self._start('tool', run_id, metadata)

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._finish(run_id)

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error)

    def failure(self, error):
        frames = [{'file': frame.filename.replace('\\', '/').rsplit('/', 1)[-1],
                   'line': frame.lineno, 'function': frame.name}
                  for frame in traceback.extract_tb(error.__traceback__)]
        self.record('worker_error', error_kind=type(error).__name__, frames=frames)
