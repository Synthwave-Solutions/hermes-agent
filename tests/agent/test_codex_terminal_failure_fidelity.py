"""Terminal Responses failures retain their cause through stream normalization."""

from types import SimpleNamespace

import pytest

from agent.codex_runtime import _consume_codex_event_stream
from agent.codex_responses_adapter import _normalize_codex_response


@pytest.mark.parametrize("status", ["failed", "cancelled"])
@pytest.mark.parametrize("output", [None, []])
def test_empty_terminal_failure_keeps_provider_cause(status, output):
    response = SimpleNamespace(
        status=status,
        output=output,
        error={"code": "rate_limit_exceeded", "message": "Slow down"},
    )
    with pytest.raises(RuntimeError, match="^rate_limit_exceeded: Slow down$"):
        _normalize_codex_response(response)


@pytest.mark.parametrize("nested_status", [None, "", "completed", "FAILED"])
@pytest.mark.parametrize("as_dict", [True, False])
def test_failed_event_is_authoritative_even_without_output(nested_status, as_dict):
    response = {
        "status": nested_status,
        "output": [],
        "error": {"code": "context_length_exceeded", "message": "Input too long"},
    }
    event = {"type": "response.failed", "response": response}
    if not as_dict:
        event = SimpleNamespace(type=event["type"], response=SimpleNamespace(**response))
    result = _consume_codex_event_stream([event], model="fixture-model")
    assert result.status == "failed"
    with pytest.raises(RuntimeError, match="^context_length_exceeded: Input too long$"):
        _normalize_codex_response(result)


def test_failed_event_without_response_has_failure_not_empty_diagnostic():
    result = _consume_codex_event_stream(
        [{"type": "response.failed"}], model="fixture-model"
    )
    assert result.status == "failed"
    with pytest.raises(RuntimeError, match="^Responses API returned status 'failed'$"):
        _normalize_codex_response(result)


def test_incomplete_content_filter_event_without_status_keeps_filter_reason():
    result = _consume_codex_event_stream(
        [{"type": "response.incomplete", "response": {
            "output": None, "incomplete_details": {"reason": "content_filter"},
        }}], model="fixture-model"
    )
    assert result.status == "incomplete"
    message, reason = _normalize_codex_response(result)
    assert reason == "content_filter"
    assert message.content == ""
    assert message.tool_calls == []


def test_failed_terminal_never_promotes_already_delivered_partial_text():
    delivered = []
    result = _consume_codex_event_stream(
        [
            {"type": "response.output_text.delta", "delta": "Partial answer"},
            {"type": "response.failed", "response": {
                "error": {"code": "server_error", "message": "Generation interrupted"},
            }},
        ], model="fixture-model", on_text_delta=delivered.append,
    )
    assert delivered == ["Partial answer"]
    assert result.output_text == "Partial answer"
    with pytest.raises(RuntimeError, match="^server_error: Generation interrupted$"):
        _normalize_codex_response(result)


def test_terminal_failure_does_not_settle_or_dispatch_pending_tool_call():
    result = _consume_codex_event_stream(
        [
            {"type": "response.output_item.added", "item": {
                "id": "fc_fixture", "type": "function_call", "name": "terminal",
                "call_id": "call_fixture", "arguments": '{"command":"synthetic"}',
            }},
            {"type": "response.failed", "response": {
                "error": {"code": "server_error", "message": "Generation interrupted"},
            }},
        ], model="fixture-model",
    )
    assert result.output == []
    with pytest.raises(RuntimeError, match="^server_error: Generation interrupted$"):
        _normalize_codex_response(result)


def test_delivered_text_without_terminal_retains_existing_eof_recovery():
    result = _consume_codex_event_stream(
        [{"type": "response.output_text.delta", "delta": "Delivered text"}],
        model="fixture-model",
    )
    message, reason = _normalize_codex_response(result)
    assert message.content == "Delivered text"
    assert reason == "stop"


def test_completed_empty_still_reports_no_output():
    result = _consume_codex_event_stream(
        [{"type": "response.completed", "response": {"output": []}}],
        model="fixture-model",
    )
    with pytest.raises(RuntimeError, match="^Responses API returned no output items$"):
        _normalize_codex_response(result)
