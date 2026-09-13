"""Actual finalizer boundaries; no provider calls or changed persistence order."""
import hashlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agent import turn_finalizer as finalizer
from tests.agent.test_background_review_trace import parent
from tests.agent.test_background_review_trace import test_actual_native_provider_loop_emits_noop_completion as _native_provider_review


def records(caplog):
    prefix='Turn finalization timing: '
    return [json.loads(r.getMessage()[len(prefix):]) for r in caplog.records
            if r.name == 'agent.turn_finalizer' and r.getMessage().startswith(prefix)]


def finish(agent, fail_sync=False):
    for name in ('_save_trajectory','_cleanup_task_resources','_persist_session','clear_interrupt',
                 '_emit_status','_safe_print','_apply_persist_user_message_override'):
        setattr(agent,name,MagicMock())
    agent._sync_external_memory_for_turn=MagicMock(side_effect=RuntimeError('private-failure') if fail_sync else None)
    agent._spawn_background_review=MagicMock()
    agent._session_messages=[]
    agent._file_mutation_verifier_enabled=lambda:False
    agent._stream_callback=None
    agent._skill_nudge_interval=15
    agent._iters_since_skill=16
    agent.valid_tool_names={'skill_manage'}
    agent.iteration_budget=SimpleNamespace(remaining=100,used=5,max_total=100)
    agent.max_iterations=50
    agent.context_compressor=None
    agent._turn_preflight_display_snapshot=None
    agent._turn_received_provider_response=False
    agent._turn_failed_file_mutations={}
    agent._db_flush_scan_prefix=None
    return finalizer.finalize_turn(agent,final_response='private-answer',api_call_count=1,
        interrupted=False,failed=False,messages=[{'role':'assistant','content':'private-answer'}],
        conversation_history=[],effective_task_id='private-task',turn_id='private-turn',
        user_message='private-user',original_user_message='private-user',
        _should_review_memory=False,_turn_exit_reason='text_response(1)')


def test_actual_finalizer_measures_whole_boundary_and_preserves_calls(parent,monkeypatch,caplog):
    caplog.set_level(logging.INFO,logger='agent.turn_finalizer')
    clock=iter((100.,109.))
    monkeypatch.setattr(finalizer,'time',SimpleNamespace(perf_counter=lambda:next(clock),time=lambda:200.))
    result=finish(parent)
    rows=records(caplog)
    assert [r['event'] for r in rows]==['started','completed']
    assert rows[-1]['duration_ms']==9000.
    assert all(r['session']==hashlib.sha256(parent.session_id.encode()).hexdigest() for r in rows)
    assert all(r['background'] is False and r['created_at']==200. for r in rows)
    assert set(rows[0])=={'event','session','background','created_at'}
    assert set(rows[1])==set(rows[0])|{'duration_ms'}
    assert 'private-' not in json.dumps(rows)
    assert result['final_response']=='private-answer'
    parent._save_trajectory.assert_called_once()
    parent._cleanup_task_resources.assert_called_once_with('private-task')
    parent._persist_session.assert_called_once()
    parent._sync_external_memory_for_turn.assert_called_once()
    parent._spawn_background_review.assert_called_once()


def test_background_fork_is_labeled_separately(parent,caplog):
    caplog.set_level(logging.INFO,logger='agent.turn_finalizer')
    parent._memory_write_origin='background_review'
    result=finish(parent)
    assert result['final_response']=='private-answer'
    assert all(r['background'] is True for r in records(caplog))
    assert len(records(caplog))==2


def test_logger_failure_cannot_change_return_or_durable_calls(parent,monkeypatch):
    monkeypatch.setattr(finalizer._timing_logger,'info',MagicMock(side_effect=OSError('private-log-error')))
    result=finish(parent)
    assert result['final_response']=='private-answer'
    parent._persist_session.assert_called_once()
    parent._sync_external_memory_for_turn.assert_called_once()
    parent._spawn_background_review.assert_called_once()


def test_clock_failure_cannot_change_result(parent,monkeypatch,caplog):
    monkeypatch.setattr(finalizer,'time',SimpleNamespace(perf_counter=MagicMock(side_effect=RuntimeError('clock')),time=lambda:200.))
    result=finish(parent)
    assert result['final_response']=='private-answer'
    assert records(caplog)==[]
    parent._persist_session.assert_called_once()


def test_native_exception_has_no_invented_completion(parent,caplog):
    caplog.set_level(logging.INFO,logger='agent.turn_finalizer')
    with pytest.raises(RuntimeError,match='private-failure'):
        finish(parent,fail_sync=True)
    assert [r['event'] for r in records(caplog)]==['started']
    parent._persist_session.assert_called_once()
    parent._spawn_background_review.assert_not_called()
    assert 'private-' not in json.dumps(records(caplog))


def test_actual_provider_loop_reaches_real_review_finalizer(parent,monkeypatch,caplog):
    caplog.set_level(logging.INFO,logger='agent.turn_finalizer')
    _native_provider_review(parent,monkeypatch,caplog)
    rows=records(caplog)
    assert [r['event'] for r in rows]==['started','completed']
    assert rows[-1]['duration_ms']>=0
    assert all(r['background'] is True for r in rows)
