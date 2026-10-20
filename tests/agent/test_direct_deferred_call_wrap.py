"""A direct call to an in-scope deferred tool runs through the tool_call bridge (SYNTHWAVE fork).

Claude via the subscription bridge called ``computer_use`` by name after ``tool_describe``;
rejecting it ended the turn without a result. The rewrite keeps the bridge's scope gate."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from run_agent import AIAgent


def _agent(valid=("read_file", "tool_call", "tool_describe", "tool_search")):
    agent = AIAgent.__new__(AIAgent)
    agent.provider, agent.model, agent.session_id, agent.tools = "custom", "m", "s1", []
    agent.valid_tool_names = set(valid)
    agent.log_prefix = ""
    agent._invalid_tool_retries = agent._invalid_json_retries = 0
    agent._vprint = agent._buffer_vprint = lambda *a, **k: None
    return agent


def _call(name, arguments):
    return SimpleNamespace(id="call_1", type="function", function=SimpleNamespace(name=name, arguments=arguments))


def _validate(agent, calls):
    from agent.turn_tool_validation import validate_tool_calls

    return validate_tool_calls(
        agent, SimpleNamespace(content="", tool_calls=calls), "tool_calls",
        messages=[], conversation_history=[], api_call_count=1, effective_task_id="t1",
    )


@pytest.fixture(autouse=True)
def _scope(monkeypatch):
    import agent.tool_executor as executor
    import hermes_cli.observability.shared_metrics_model as metrics

    monkeypatch.setattr(metrics, "record_tool_call_quality", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_tool_search_scoped_names", lambda agent: frozenset({"computer_use"}))


def test_in_scope_deferred_call_is_routed_through_tool_call():
    call = _call("computer_use", '{"action": "capture"}')
    verdict = _validate(_agent(), [call])
    assert verdict.action == "ok"
    assert call.function.name == "tool_call"
    assert json.loads(call.function.arguments) == {"calls": [{"name": "computer_use", "arguments": {"action": "capture"}}]}


def test_empty_arguments_become_an_empty_object():
    call = _call("computer_use", "")
    assert _validate(_agent(), [call]).action == "ok"
    assert json.loads(call.function.arguments)["calls"][0]["arguments"] == {}


def _wrap(agent, call):
    from agent.turn_tool_validation import _wrap_direct_deferred_call

    return _wrap_direct_deferred_call(agent, call, agent.valid_tool_names)


def test_out_of_scope_name_stays_unknown():
    call = _call("browser_secret_dump", "{}")
    assert _wrap(_agent(), call) is False
    assert call.function.name == "browser_secret_dump"


def test_no_rewrite_without_the_tool_call_bridge():
    call = _call("computer_use", "{}")
    assert _wrap(_agent(valid=("read_file",)), call) is False
    assert call.function.name == "computer_use"


def test_advertised_tool_is_untouched():
    call = _call("read_file", '{"path": "a"}')
    assert _validate(_agent(), [call]).action == "ok"
    assert call.function.name == "read_file"


def test_non_object_arguments_are_not_rewritten():
    call = _call("computer_use", "[1, 2]")
    assert _wrap(_agent(), call) is False
    assert call.function.name == "computer_use"
