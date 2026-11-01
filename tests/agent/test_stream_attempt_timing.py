"""Native main-stream timing without provider calls or prompt logging."""
import hashlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from tests.agent.test_router_provider_wait_timeout import make_agent


@pytest.fixture(autouse=True)
def refuse_http(monkeypatch):
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('Unexpected HTTP in native stream fixture')

    async def refuse_async(*args, **kwargs):
        return refuse(*args, **kwargs)

    monkeypatch.setattr(httpx.Client, 'send', refuse)
    monkeypatch.setattr(httpx.AsyncClient, 'send', refuse_async)
    yield
    assert attempts == [], 'A swallowed exception concealed network access'


def chunk(text=None, reasoning=None, tool=None):
    return SimpleNamespace(choices=[SimpleNamespace(index=0,
        delta=SimpleNamespace(content=text, tool_calls=tool,
                              reasoning_content=reasoning, reasoning=None),
        finish_reason='tool_calls' if tool else 'stop')],
        model='codex/gpt-6-astra', usage=None)


def setup_agent(monkeypatch, tmp_path, caplog):
    agent = make_agent(monkeypatch, tmp_path, 'custom', 'custom:omniroute')
    agent.session_id = 'private-session-fixture'
    agent._current_api_request_id = 'private-request-fixture'
    agent._api_call_count = 2
    monkeypatch.setattr(agent, '_close_request_openai_client', lambda *a, **k: None)
    caplog.set_level(logging.INFO, logger='agent.stream_diag')
    clock = [10.0]
    monkeypatch.setattr('agent.stream_diag._timing_clock', lambda: clock[0], raising=False)
    return agent, clock


def records(caplog):
    return [json.loads(row.getMessage().split(' ', 1)[1])
            for row in caplog.records
            if row.name == 'agent.stream_diag'
            and row.getMessage().startswith('stream_attempt_timing ')]


def invoke(agent):
    return agent._interruptible_streaming_api_call({
        'model': agent.model, 'messages': [{'role': 'user', 'content': 'private-input-fixture'}],
        'reasoning_effort': 'high'})


def test_native_success_separates_dispatch_headers_reasoning_and_text(monkeypatch, tmp_path, caplog):
    agent, clock = setup_agent(monkeypatch, tmp_path, caplog)
    delivered = []
    monkeypatch.setattr(agent, '_fire_stream_delta', delivered.append)

    class Stream:
        response = SimpleNamespace(status_code=200, headers={'x-request-id': 'private-header-fixture'})

        def __iter__(self):
            clock[0] = 14.0
            yield chunk()
            clock[0] = 16.0
            yield chunk(reasoning='private-reasoning-fixture')
            clock[0] = 20.0
            yield chunk('private-output-fixture')
            clock[0] = 21.0

        def close(self):
            pass

    received = []
    client = MagicMock()

    def create_stream(**kwargs):
        received.append(kwargs)
        clock[0] = 13.0
        return Stream()

    client.chat.completions.create.side_effect = create_stream

    def create_client(**kwargs):
        clock[0] = 11.0
        return client

    monkeypatch.setattr(agent, '_create_request_openai_client', create_client)
    response = invoke(agent)
    rows = records(caplog)
    assert len(rows) == 1, 'Successful streams need one bounded timing record'
    row = rows[0]
    assert row['outcome'] == 'success'
    assert row['sdk_dispatch_ms'] == 1000
    assert row['response_headers_ms'] == 3000
    assert row['first_chunk_ms'] == 4000
    assert row['first_reasoning_chunk_ms'] == 6000
    assert row['first_text_dispatch_ms'] == 10000
    assert row['end_ms'] == 11000
    assert row['sdk_requests'] == 1 and row['chunks'] == 3
    assert row['api_call_count'] == 2 and row['stream_attempt'] == 1
    assert row['session_hash'] == hashlib.sha256(agent.session_id.encode()).hexdigest()[:16]
    assert row['api_request_hash'] == hashlib.sha256(agent._current_api_request_id.encode()).hexdigest()[:16]
    assert row['http_status'] == 200
    assert 'private-' not in json.dumps(row)
    assert received[0]['reasoning_effort'] == 'high'
    assert response.choices[0].message.content == 'private-output-fixture'
    assert delivered == ['private-output-fixture']


def test_native_retry_keeps_attempts_separate_and_unknown_fields_null(monkeypatch, tmp_path, caplog):
    agent, clock = setup_agent(monkeypatch, tmp_path, caplog)
    monkeypatch.setattr('agent.chat_completion_helpers.env_int', lambda name, default: 1 if name == 'HERMES_STREAM_RETRIES' else default)
    client = MagicMock()
    calls = []

    def create_stream(**kwargs):
        calls.append(True)
        if len(calls) == 1:
            clock[0] = 11.0
            raise httpx.ReadTimeout('private-error-fixture')
        return iter([chunk('recovered')])

    client.chat.completions.create.side_effect = create_stream
    monkeypatch.setattr(agent, '_create_request_openai_client', lambda **k: client)
    assert invoke(agent).choices[0].message.content == 'recovered'
    rows = records(caplog)
    assert [row['outcome'] for row in rows] == ['error', 'success']
    assert [row['stream_attempt'] for row in rows] == [1, 2]
    assert rows[0]['first_chunk_ms'] is None
    assert rows[0]['response_headers_ms'] is None
    assert rows[0]['first_text_dispatch_ms'] is None
    assert rows[1]['response_headers_ms'] is None
    assert rows[1]['http_status'] is None
    assert len(calls) == 2


def test_native_cancel_does_not_retry_or_log_success(monkeypatch, tmp_path, caplog):
    agent, clock = setup_agent(monkeypatch, tmp_path, caplog)
    client = MagicMock()

    def cancel(**kwargs):
        agent._interrupt_requested = True
        raise httpx.ReadError('private-cancel-fixture')

    client.chat.completions.create.side_effect = cancel
    monkeypatch.setattr(agent, '_create_request_openai_client', lambda **k: client)
    with pytest.raises(InterruptedError):
        invoke(agent)
    rows = records(caplog)
    assert len(rows) == 1 and rows[0]['outcome'] == 'cancelled'
    assert rows[0]['first_chunk_ms'] is None
    assert client.chat.completions.create.call_count == 1


def test_native_tool_only_keeps_text_observation_unknown(monkeypatch, tmp_path, caplog):
    agent, clock = setup_agent(monkeypatch, tmp_path, caplog)
    client = MagicMock()
    tool = SimpleNamespace(index=0, id='private-tool-id', type='function',
        function=SimpleNamespace(name='private-tool-name', arguments='{"private-argument":true}'))
    client.chat.completions.create.return_value = iter([chunk(tool=[tool])])
    monkeypatch.setattr(agent, '_create_request_openai_client', lambda **k: client)
    response = invoke(agent)
    assert response.choices[0].message.tool_calls[0].function.name == 'private-tool-name'
    row, = records(caplog)
    assert row['first_chunk_ms'] == 0
    assert row['first_text_dispatch_ms'] is None
    assert row['first_reasoning_chunk_ms'] is None
    assert 'private-' not in json.dumps(row)


def test_native_logger_failure_preserves_answer(monkeypatch, tmp_path, caplog):
    agent, clock = setup_agent(monkeypatch, tmp_path, caplog)
    client = MagicMock()
    client.chat.completions.create.return_value = iter([chunk('unchanged')])
    monkeypatch.setattr(agent, '_create_request_openai_client', lambda **k: client)

    def broken(*args, **kwargs):
        raise OSError('synthetic log destination failed')

    monkeypatch.setattr('agent.stream_diag.logger.info', broken)
    assert invoke(agent).choices[0].message.content == 'unchanged'
    assert client.chat.completions.create.call_count == 1


def test_completed_response_adapter_does_not_invent_headers_or_chunks(monkeypatch, tmp_path, caplog):
    agent, clock = setup_agent(monkeypatch, tmp_path, caplog)
    client = MagicMock()
    complete = SimpleNamespace(id='fixture-complete', model=agent.model, usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content='completed adapter',
            reasoning_content=None, reasoning=None, tool_calls=None), finish_reason='stop')])
    client.chat.completions.create.return_value = complete
    monkeypatch.setattr(agent, '_create_request_openai_client', lambda **k: client)
    assert invoke(agent) is complete
    assert agent._disable_streaming is True
    row, = records(caplog)
    assert row['outcome'] == 'success'
    assert row['chunks'] == 0
    assert row['first_chunk_ms'] is None and row['response_headers_ms'] is None
    assert row['first_text_dispatch_ms'] == 0


def test_attempt_binding_is_immutable_and_summary_is_idempotent(monkeypatch, caplog):
    from agent import stream_diag as d
    caplog.set_level(logging.INFO, logger='agent.stream_diag')
    agent = SimpleNamespace(session_id='old-private-session', _current_api_request_id='old-private-request', _api_call_count=1)
    clock = [4.0]
    monkeypatch.setattr(d, '_timing_clock', lambda: clock[0])
    diag = d.stream_diag_init()
    d.stream_diag_start_timing(agent, diag)
    agent.session_id = 'new-private-session'
    agent._current_api_request_id = 'new-private-request'
    agent._api_call_count = 9
    d.stream_diag_mark_timing(diag, 'sdk_dispatch')
    clock[0] = 5.0
    d.stream_diag_mark_timing(diag, 'sdk_dispatch')
    d.stream_diag_mark_timing(diag, 'private-unapproved-field')
    d.log_stream_attempt_timing(diag, attempt=1, outcome='success')
    d.log_stream_attempt_timing(diag, attempt=1, outcome='error')
    row, = records(caplog)
    assert row['session_hash'] == hashlib.sha256(b'old-private-session').hexdigest()[:16]
    assert row['api_request_hash'] == hashlib.sha256(b'old-private-request').hexdigest()[:16]
    assert row['api_call_count'] == 1
    assert row['sdk_requests'] == 2 and row['sdk_dispatch_ms'] == 0
    assert row['end_ms'] == 1000
    assert 'private-' not in json.dumps(row)


def test_unknown_or_nonfinite_observations_are_null(monkeypatch, caplog):
    from agent import stream_diag as d
    caplog.set_level(logging.INFO, logger='agent.stream_diag')
    diag = d.stream_diag_init()
    d.stream_diag_start_timing(SimpleNamespace(), diag)
    diag['http_status'] = 'private-header'
    diag['chunks'] = False
    diag['_timing_offsets']['response_headers'] = float('nan')
    diag['_timing_offsets']['first_chunk'] = float('inf')
    d.log_stream_attempt_timing(diag, attempt=1, outcome='success')
    row, = records(caplog)
    for field in ('session_hash', 'api_request_hash', 'api_call_count', 'http_status', 'chunks', 'response_headers_ms', 'first_chunk_ms'):
        assert row[field] is None
    assert 'private-' not in json.dumps(row)
