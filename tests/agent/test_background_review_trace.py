"""Native gate and detached review diagnostics without external requests."""
import hashlib
import json
import logging
import threading
from unittest.mock import MagicMock

import httpx
import pytest
from agent import background_review as review
from agent.turn_finalizer import finalize_turn
from run_agent import AIAgent


@pytest.fixture
def parent(monkeypatch, tmp_path, caplog):
    attempts = []
    def refuse(*a, **kw):
        attempts.append(True)
        raise AssertionError('Unexpected external request')
    monkeypatch.setattr(httpx.Client, 'send', refuse)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path / 'config.yaml').write_text('{}\n')
    for name in ('detect_local_server_type', '_query_local_context_length', '_query_ollama_api_show'):
        monkeypatch.setattr('agent.model_metadata.' + name, lambda *a, **k: None)
    monkeypatch.setattr('agent.model_metadata.fetch_endpoint_model_metadata', lambda *a, **k: {})
    monkeypatch.setattr('run_agent.get_tool_definitions', lambda *a, **k: [])
    monkeypatch.setattr('run_agent.check_toolset_requirements', lambda *a, **k: {})
    monkeypatch.setattr('model_tools.get_tool_definitions', lambda *a, **k: [])
    agent = AIAgent(model='synthetic-model', provider='custom', requested_provider='custom:omniroute',
        api_key='private-fixture-key', base_url='http://127.0.0.1:20128/v1',
        api_mode='chat_completions', quiet_mode=True, skip_context_files=True,
        skip_memory=True, session_id='private-synthetic-session', platform='webui')
    agent._cached_system_prompt = 'private-prompt-do-not-log'
    caplog.set_level(logging.INFO, logger='agent.background_review')
    yield agent
    agent.close()
    assert attempts == []


def traces(caplog):
    prefix = 'Background review trace: '
    return [json.loads(r.getMessage()[len(prefix):]) for r in caplog.records
            if r.name == 'agent.background_review' and r.getMessage().startswith(prefix)]


def finish(agent, *, available=True, counter=16, skip=False):
    for name in ('_save_trajectory', '_cleanup_task_resources', '_persist_session',
                 'clear_interrupt', '_sync_external_memory_for_turn', '_emit_status',
                 '_safe_print', '_apply_persist_user_message_override'):
        setattr(agent, name, MagicMock())
    agent._spawn_background_review = MagicMock()
    agent._session_messages = []
    agent._file_mutation_verifier_enabled = lambda: False
    agent._stream_callback = None
    agent._skill_nudge_interval = 15
    agent._iters_since_skill = counter
    agent.skip_background_review = skip
    agent.valid_tool_names = {'skill_manage'} if available else {'terminal'}
    agent.iteration_budget = MagicMock(remaining=100, used=5, max_total=100)
    agent.max_iterations = 50
    agent.context_compressor = None
    agent._turn_preflight_display_snapshot = None
    agent._turn_received_provider_response = False
    agent._turn_failed_file_mutations = {}
    agent._db_flush_scan_prefix = None
    return finalize_turn(agent, final_response='private-answer', api_call_count=1,
        interrupted=False, failed=False, messages=[{'role':'assistant','content':'private-answer'}],
        conversation_history=[], effective_task_id='private-task', turn_id='private-turn',
        user_message='private-user-message', original_user_message='private-user-message',
        _should_review_memory=False, _turn_exit_reason='text_response(1)')


@pytest.mark.parametrize('available,counter,skip,eligible', [
    (False,16,False,False), (True,14,False,False),
    (True,16,False,True), (True,16,True,False),
])
def test_actual_finalizer_logs_tool_availability_and_counter_before_reset(
        parent, caplog, available, counter, skip, eligible):
    result = finish(parent, available=available, counter=counter, skip=skip)
    gate = [r for r in traces(caplog) if r['event'] == 'gate']
    assert len(gate) == 1
    assert gate[0]['skill_available'] is available
    assert gate[0]['skill_counter'] == counter
    assert gate[0]['skill_interval'] == 15
    assert gate[0]['eligible'] is eligible
    assert parent._spawn_background_review.call_count == int(eligible)
    assert parent._iters_since_skill == (0 if available and counter >= 15 else counter)
    assert result['final_response'] == 'private-answer'
    assert gate[0]['session'] == hashlib.sha256(parent.session_id.encode()).hexdigest()
    assert 'private-' not in json.dumps(traces(caplog))


@pytest.mark.parametrize('disabled,delegated,reason', [
    (True,False,'disabled'), (False,True,'delegated'),
])
def test_actual_spawn_skip_is_observable(parent, monkeypatch, caplog, disabled, delegated, reason):
    monkeypatch.setattr(review, 'load_background_review_settings', lambda: (not disabled, {}))
    parent._delegate_depth = int(delegated)
    parent._spawn_background_review(messages_snapshot=[], review_skills=True)
    rows = traces(caplog)
    assert [(r['event'],r.get('reason')) for r in rows] == [('skipped',reason)]
    assert getattr(parent,'_background_review_run',None) is None


class ImmediateThread:
    def __init__(self, *, target, **kwargs): self.target = target
    def start(self): self.target()


@pytest.mark.parametrize('outcome', ['noop','failed','cancelled','exception'])
def test_native_fork_reports_actual_exit_without_private_content(
        parent, monkeypatch, caplog, outcome):
    monkeypatch.setattr('run_agent.threading.Thread', ImmediateThread)
    def response(child, **kwargs):
        assert child is not parent
        assert child.session_id == parent.session_id
        assert child._cached_system_prompt == parent._cached_system_prompt
        child._session_messages = []
        if outcome == 'exception': raise RuntimeError('private-provider-error')
        if outcome == 'cancelled': parent._background_review_run.cancel()
        return {'failed':outcome == 'failed','interrupted':outcome == 'cancelled'}
    monkeypatch.setattr(AIAgent, 'run_conversation', response)
    parent._spawn_background_review(messages_snapshot=[], review_skills=True)
    rows = traces(caplog)
    assert [r['event'] for r in rows] == [
        'scheduled','started','request_started',
        {'noop':'completed','failed':'failed','cancelled':'cancelled','exception':'failed'}[outcome]]
    assert len({r['review'] for r in rows}) == 1
    assert all(r['session'] == hashlib.sha256(parent.session_id.encode()).hexdigest() for r in rows)
    assert 'private-' not in json.dumps(rows)
    assert parent._background_review_run is None


def test_trace_logging_failure_cannot_block_native_gate(parent, monkeypatch):
    monkeypatch.setattr(review.logger, 'info', MagicMock(side_effect=OSError('private-log-failure')))
    finish(parent)
    parent._spawn_background_review.assert_called_once()


def test_actual_native_provider_loop_emits_noop_completion(parent, monkeypatch, caplog):
    native_thread = threading.Thread
    monkeypatch.setattr('run_agent.threading.Thread', lambda *args, **kw:
                        ImmediateThread(**kw) if kw.get('name') == 'bg-review' else native_thread(*args, **kw))
    calls = []
    def response(client, request, **kwargs):
        calls.append(request.url.path)
        assert request.url.path == '/v1/chat/completions'
        if json.loads(request.content).get('stream'):
            chunks = [
                {'id':'synthetic-completion','object':'chat.completion.chunk','created':1,'model':'synthetic-model',
                 'choices':[{'index':0,'delta':{'role':'assistant','content':'No useful change.'},'finish_reason':None}]},
                {'id':'synthetic-completion','object':'chat.completion.chunk','created':1,'model':'synthetic-model',
                 'choices':[{'index':0,'delta':{},'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}},
            ]
            content = ''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
            return httpx.Response(200, request=request, headers={'Content-Type':'text/event-stream'}, content=content)
        return httpx.Response(200, request=request, json={
            'id':'synthetic-completion','object':'chat.completion','created':1,'model':'synthetic-model',
            'choices':[{'index':0,'message':{'role':'assistant','content':'No useful change.'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}})
    monkeypatch.setattr(httpx.Client, 'send', response)
    parent._spawn_background_review(messages_snapshot=[], review_skills=True)
    rows = traces(caplog)
    lifecycle = [r for r in rows if 'review' in r]
    assert [r['event'] for r in lifecycle] == ['scheduled','started','request_started','completed']
    assert lifecycle[-1]['result'] == 'none'
    assert calls == ['/v1/chat/completions']
    child_gates = [r for r in rows if r['event'] == 'gate']
    assert len(child_gates) == 1 and child_gates[0]['background'] is True
    assert child_gates[0]['eligible'] is False
    assert 'private-' not in json.dumps(rows)


def test_active_reservation_and_real_thread_cancellation(parent, monkeypatch, caplog):
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    def response(child, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'interrupted':True}
    monkeypatch.setattr(AIAgent, 'run_conversation', response)
    monkeypatch.setattr('agent.interrupt_compat.request_hard_interrupt', lambda *a, **kw: release.set())
    original_trace = review.trace_background_review
    def observe(*args, **kwargs):
        original_trace(*args, **kwargs)
        if len(args) > 1 and args[1] == 'cancelled': exited.set()
    monkeypatch.setattr(review, 'trace_background_review', observe)
    parent._spawn_background_review(messages_snapshot=[], review_skills=True)
    try:
        assert entered.wait(5)
        parent._spawn_background_review(messages_snapshot=[], review_skills=True)
        review.cancel_background_review_for_live_turn(parent)
        assert exited.wait(5)
    finally:
        release.set()
    rows = traces(caplog)
    assert any(r['event']=='skipped' and r['reason']=='reservation_unavailable' for r in rows)
    assert [r['event'] for r in rows if r['event'] in {'cancel_requested','cancelled'}] == ['cancel_requested','cancelled']
    assert sum(r['event']=='request_started' for r in rows) == 1
    assert parent._background_review_run is None


def test_closed_trace_fields_reject_private_values(parent, caplog):
    review.trace_background_review(parent, 'skipped', reason='private-reason',
                                   result='private-skill-name', api_key='private-key',
                                   skill_counter='private-counter')
    rows = traces(caplog)
    assert len(rows) == 1 and set(rows[0]) == {'event','session','background'}
    assert 'private-' not in json.dumps(rows)


def test_native_request_trace_reaches_rotating_file_during_thread_silence(
        parent, monkeypatch, tmp_path):
    import sys
    from hermes_logging import _ManagedRotatingFileHandler
    from agent.redact import RedactingFormatter

    path = tmp_path / 'review-trace.log'
    handler = _ManagedRotatingFileHandler(path, maxBytes=16384, backupCount=1)
    handler.setFormatter(RedactingFormatter('%(message)s'))
    review.logger.addHandler(handler)
    monkeypatch.setattr('run_agent.threading.Thread', ImmediateThread)
    def response(child, **kwargs):
        assert sys.stdout._state.silenced.get(threading.get_ident(), 0) > 0
        return {'failed':False, 'interrupted':False}
    monkeypatch.setattr(AIAgent, 'run_conversation', response)
    try:
        parent._spawn_background_review(messages_snapshot=[], review_skills=True)
        handler.flush()
        prefix = 'Background review trace: '
        rows = [json.loads(line[len(prefix):]) for line in path.read_text().splitlines()
                if line.startswith(prefix)]
        assert [row['event'] for row in rows] == [
            'scheduled', 'started', 'request_started', 'completed']
        assert len({row['review'] for row in rows}) == 1
        assert 'private-' not in json.dumps(rows)
    finally:
        review.logger.removeHandler(handler)
        handler.close()
