"""A terminal Responses frame ends network reads, including Relay finalization."""

from types import SimpleNamespace

import pytest

from agent import relay_runtime
from agent.codex_runtime import _TerminalBoundedCodexStream, run_codex_stream


@pytest.fixture(params=[False, True], ids=["direct", "managed-relay"])
def managed_turn(request, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    if not request.param:
        yield None
        relay_runtime._reset_for_tests()
        return
    pytest.importorskip("nemo_relay")
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(), session_id="fixture-session", platform="webui",
    )
    turn = relay_runtime.SESSION_COORDINATOR.begin_turn(
        lease, turn_id="fixture-turn", task_id="fixture-task",
    )
    lease.host.retain_managed_execution("test.codex_terminal_boundary")
    finalizations = []
    original_execute = lease.host.relay.llm.stream_execute

    async def observe_finalization(name, req, provider, observer, finalizer, **kwargs):
        def counted_finalizer():
            value = finalizer()
            finalizations.append(value)
            return value
        return await original_execute(name, req, provider, observer, counted_finalizer, **kwargs)

    monkeypatch.setattr(lease.host.relay.llm, "stream_execute", observe_finalization)
    try:
        yield finalizations
    finally:
        lease.host.release_managed_execution("test.codex_terminal_boundary")
        relay_runtime.SESSION_COORDINATOR.end_turn(turn, outcome="success")
        relay_runtime.SESSION_COORDINATOR.release_conversation(lease)
        relay_runtime._reset_for_tests()


@pytest.mark.parametrize("terminal", ["completed", "incomplete", "failed"])
def test_no_provider_read_after_terminal_frame(managed_turn, terminal):
    delivered = []
    usage = SimpleNamespace(input_tokens=10, output_tokens=2, total_tokens=12)

    class HangingAfterTerminal:
        """The next network read would block; fail immediately to prove absence."""

        def __init__(self):
            self.events = iter([
                SimpleNamespace(type="response.output_text.delta", delta="Available answer"),
                SimpleNamespace(type=f"response.{terminal}", response=SimpleNamespace(
                    status=terminal, usage=usage, id="fixture-response",
                    incomplete_details={"reason": "max_output_tokens"},
                    error={"code": "server_error", "message": "Fixture failure"},
                )),
            ])
            self.close_calls = 0
            self.after_terminal_reads = 0

        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(self.events)
            except StopIteration:
                self.after_terminal_reads += 1
                raise AssertionError("Unnecessary network read after terminal response")

        def close(self):
            self.close_calls += 1

    raw = HangingAfterTerminal()
    requests = []

    def create(**kwargs):
        requests.append(kwargs)
        return raw

    agent = SimpleNamespace(
        _interrupt_requested=False, session_id="fixture-session", provider="custom",
        model="fixture-model", _current_api_request_id="fixture-request",
        _fire_stream_delta=delivered.append, _fire_reasoning_delta=lambda _: None,
        _touch_activity=lambda _: None, _client_log_context=lambda: "fixture",
        _abort_request_openai_client=lambda *args, **kwargs: None,
    )
    result = run_codex_stream(
        agent,
        {"model": "fixture-model", "input": [], "instructions": "Fixture", "store": False},
        client=SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    assert len(requests) == 1
    assert raw.after_terminal_reads == 0
    assert raw.close_calls == 1
    assert delivered == ["Available answer"]
    assert result.output_text == "Available answer"
    assert result.id == "fixture-response"
    assert result.usage is usage
    assert result.status == terminal
    if managed_turn is not None:
        assert len(managed_turn) == 1
        assert managed_turn[0]["status"] == terminal
        assert managed_turn[0]["usage"]["total_tokens"] == 12


def test_close_before_first_event_closes_raw_resource_once():
    closed = []
    raw = SimpleNamespace(__unused=True)

    class UnopenedStream:
        def __iter__(self):
            return iter(())

        def close(self):
            closed.append(raw)

    bounded = _TerminalBoundedCodexStream(UnopenedStream())
    bounded.close()
    bounded.close()
    assert list(bounded) == []
    assert closed == [raw]


def test_malformed_nonterminal_type_does_not_crash_or_end_the_stream():
    events = [{"type": []}, {"type": "response.completed"}, {"type": "not-read"}]
    assert list(_TerminalBoundedCodexStream(iter(events))) == events[:2]
