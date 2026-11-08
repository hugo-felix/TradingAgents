import json
from uuid import uuid4

from tradingagents.desktop.diagnostics import RunDiagnostics


def test_log_write_failure_preserves_activity_and_error_notifications(tmp_path):
    events = []
    log = RunDiagnostics(tmp_path, lambda kind, **fields: events.append((kind, fields)))
    log.record('worker_start')
    log.failure(ValueError('private error'))
    assert [fields['activity'] for _kind, fields in events] == ['worker_start', 'worker_error']


def test_diagnostics_correlates_parallel_calls_and_excludes_sensitive_content(tmp_path):
    events = []
    path = tmp_path / 'diagnostics.jsonl'
    log = RunDiagnostics(path, lambda kind, **fields: events.append((kind, fields)))
    first, second = uuid4(), uuid4()
    log.on_chat_model_start({'secret': 'token'}, ['private prompt'], run_id=first,
                            metadata={'langgraph_node': 'Bull Researcher', 'api_key': 'token'})
    log.on_chat_model_start({}, ['private prompt'], run_id=second,
                            metadata={'langgraph_node': 'Bear Researcher'})
    log.on_llm_end('private answer', run_id=second)
    error = ValueError('secret credential and server response')
    log.on_llm_error(error, run_id=first)
    log.on_tool_start({'name': 'tool'}, 'private arguments', run_id=first)
    log.on_tool_end('private financial data', run_id=first)
    try:
        raise error
    except ValueError as exc:
        log.failure(exc)
    raw = path.read_text(encoding='utf-8')
    assert all(secret not in raw for secret in ('private', 'credential', 'token', 'server response'))
    rows = [json.loads(line) for line in raw.splitlines()]
    assert rows[2]['call_id'] == str(second)
    assert rows[2]['node'] == 'Bear Researcher'
    assert rows[3]['node'] == 'Bull Researcher'
    assert rows[3]['error_kind'] == 'ValueError'
    assert rows[3]['elapsed_seconds'] >= 0
    assert rows[-1]['frames'][-1]['function'] == (
        'test_diagnostics_correlates_parallel_calls_and_excludes_sensitive_content')
    assert all(kind == 'activity' for kind, _fields in events)


def test_model_response_shape_is_logged_without_provider_text(tmp_path):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    path = tmp_path / 'diagnostics.jsonl'
    log = RunDiagnostics(path, lambda *args, **kwargs: None)
    run_id = uuid4()
    log.on_chat_model_start({}, [], run_id=run_id)
    response = LLMResult(generations=[[ChatGeneration(
        message=AIMessage(content='', additional_kwargs={'reasoning_content': 'private'}),
        generation_info={'finish_reason': 'length'},
    )]])
    log.on_llm_end(response, run_id=run_id)
    raw = path.read_text(encoding='utf-8')
    assert 'private' not in raw
    assert json.loads(raw.splitlines()[-1])['responses'] == [
        {'content_chars': 0, 'tool_calls': 0, 'finish_reason': 'length', 'tokens': {}}]
