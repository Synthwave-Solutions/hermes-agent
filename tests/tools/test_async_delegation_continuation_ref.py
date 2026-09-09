"""Only the trusted host can attach a continuation principal reference."""
import json
import queue
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

from tools import async_delegation as delegation


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(delegation, "_db_path", lambda: path)
    monkeypatch.setattr(delegation, "_records", {})
    return path


def record(ref):
    return {"delegation_id": "deleg_fixture", "session_key": "owned-ui",
            "origin_ui_session_id": "owned-ui", "parent_session_id": "owned-ui",
            "dispatched_at": time.time(), "goal": "Fixture work", "goals": ["Fixture work"],
            "webui_continuation_ref": ref}


def test_ref_is_only_captured_from_loaded_host_context(monkeypatch):
    monkeypatch.delitem(sys.modules, "api.governance.continuation", raising=False)
    monkeypatch.setenv("WEBUI_CONTINUATION_REF", "f" * 32)
    assert "webui_continuation_ref" not in delegation._capture_routing_origin()
    monkeypatch.setitem(sys.modules, "api.governance.continuation", SimpleNamespace(current_ref=lambda: "a" * 32))
    assert delegation._capture_routing_origin()["webui_continuation_ref"] == "a" * 32
    monkeypatch.setitem(sys.modules, "api.governance.continuation", SimpleNamespace(current_ref=lambda: "malformed"))
    with pytest.raises(ValueError): delegation._capture_routing_origin()


@pytest.mark.parametrize("batch", [False, True])
def test_ref_survives_dispatch_ledger_and_completed_event(ledger, monkeypatch, batch):
    from tools.process_registry import process_registry
    target = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", target)
    ref = "b" * 32
    row = record(ref)
    delegation._persist_dispatch(row)
    with sqlite3.connect(ledger) as conn:
        task = json.loads(conn.execute("SELECT task_json FROM async_delegations").fetchone()[0])
    assert task["webui_continuation_ref"] == ref
    result = {"summary": "Completed fixture", "results": [{"status": "completed", "summary": "Completed fixture"}]}
    if batch: delegation._push_batch_completion_event(row, result, "completed")
    else: delegation._push_completion_event(row, result, "completed")
    event = target.get_nowait()
    assert event["webui_continuation_ref"] == ref
    with sqlite3.connect(ledger) as conn:
        saved = json.loads(conn.execute("SELECT event_json FROM async_delegations").fetchone()[0])
    assert saved["webui_continuation_ref"] == ref
    assert set(saved).isdisjoint({"cookie", "identity", "governance_context", "groups"})


def test_recovery_keeps_ref_but_cannot_invent_an_identity(ledger, monkeypatch):
    from gateway import status
    monkeypatch.setattr(status, "_pid_exists", lambda pid: False)
    ref = "c" * 32
    delegation._persist_dispatch(record(ref))
    assert delegation.recover_abandoned_delegations() == 1
    with sqlite3.connect(ledger) as conn:
        event = json.loads(conn.execute("SELECT event_json FROM async_delegations").fetchone()[0])
    assert event["webui_continuation_ref"] == ref
    assert event["status"] == "unknown"
    assert "identity" not in event
