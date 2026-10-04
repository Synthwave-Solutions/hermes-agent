from tools.delegation_progress import normalize_subagent_progress as normalize
import pytest

@pytest.mark.parametrize("phase,state", [("spawn_requested","queued"),("start","running"),("tool_call","running"),("complete","completed")])
def test_lifecycle(phase,state):
    out=normalize("subagent."+phase, {"subagent_id":"child-1","status":"success","goal":"Review tests", "args":{"secret":"hidden"},"reasoning":"private"})
    assert out["status"]==state
    assert out["summary"]=="Review tests"
    assert "args" not in out and "reasoning" not in out
    assert normalize("subagent",out)==out

@pytest.mark.parametrize("phase", ["thinking","text","unknown"])
def test_private_events_rejected(phase):
    assert normalize("subagent."+phase,{"subagent_id":"child-1"}) is None

def test_unknown_completion_is_not_success():
    assert normalize("subagent.complete",{"id":"x"})["status"]=="failed"

def test_bounded_and_redacted():
    out=normalize("subagent.start",{"id":"x", "goal":"token=abc sk-secret review\nprivate", "tool_count":-2,"duration_seconds":float("nan")})
    assert "abc" not in out["summary"] and "sk-secret" not in out["summary"]
    assert out["tool_count"]==0 and "duration_seconds" not in out


def test_real_child_callback_relays_queued_with_identity():
    from tools.delegate_tool import _build_child_progress_callback
    from types import SimpleNamespace
    rows=[]
    def callback(event,name=None,preview=None,args=None,**kwargs):
        rows.append(normalize(event,kwargs))
    child=_build_child_progress_callback(1,"Review isolated package",SimpleNamespace(tool_progress_callback=callback),task_count=3,subagent_id="child-2",parent_id="parent-1")
    child("subagent.spawn_requested")
    child("subagent.start")
    child("subagent.complete",status="success")
    assert [r["status"] for r in rows]==["queued","running","completed"]
    assert all(r["id"]=="child-2" and r["task_index"]==1 and r["task_count"]==3 for r in rows)
