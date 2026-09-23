"""Loop-level tests for routed-failover compaction (SYNTHWAVE fork).

A router filters fallback targets whose window is too small before dispatch.
When the large-window primary then fails transiently, the loop must compact to
the fallback window and retry instead of failing the turn.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from run_agent import AIAgent
from tests.run_agent.raw_response_mock import wire_raw_response

ROUTED_OPUS = "openai-compatible-chat-test/claude-opus-5-5"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)


def _ok(content="answered by the fallback"):
    msg = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None, reasoning=None)
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model="test/model")
    resp.usage = None
    return resp


def _rate_limited():
    err = Exception("Rate limit exceeded: bridge busy")
    err.status_code = 429
    return err


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    a.client = wire_raw_response(MagicMock())
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.compression_enabled = True
    a.save_trajectories = False
    # A previous answer came from the 1M Opus route.
    a.context_compressor.update_model(
        model=ROUTED_OPUS, context_length=1_000_000, base_url=a.base_url,
        api_key="test-key-1234567890", provider=a.provider, api_mode=a.api_mode,
    )
    a._effective_routed_model = ROUTED_OPUS
    return a


def _run(agent, request_tokens):
    """First call is rate limited, the retry succeeds; returns the compressor
    window and routed-model marker seen by the retry request."""
    seen = {}

    def create(**_kwargs):
        if not seen:
            seen["first"] = True
            raise _rate_limited()
        seen["retry_context_length"] = agent.context_compressor.context_length
        seen["retry_routed_model"] = agent._effective_routed_model
        return _ok()

    agent.client.chat.completions.create.side_effect = create
    history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    ]
    with (
        patch("agent.conversation_loop.estimate_request_tokens_rough", return_value=request_tokens),
        patch.object(agent, "_compress_context") as compress,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        compress.return_value = ([{"role": "user", "content": "hello"}], "compressed prompt")
        result = agent.run_conversation("hello", conversation_history=history)
    return result, compress, seen


def test_large_request_compacts_to_fallback_window_and_retries(agent):
    agent._failover_context_length = 272_000

    result, compress, seen = _run(agent, 400_000)

    compress.assert_called_once()
    assert result["completed"] is True
    assert result["final_response"] == "answered by the fallback"
    assert seen["retry_context_length"] == 272_000
    # The routed window is re-resolved from the next answer, not persisted.
    assert seen["retry_routed_model"] is None
    assert agent.context_compressor._context_probe_persistable is False


def test_request_that_fits_the_fallbacks_is_not_compacted(agent):
    agent._failover_context_length = 272_000

    result, compress, seen = _run(agent, 150_000)

    compress.assert_not_called()
    assert result["completed"] is True
    assert seen["retry_context_length"] == 1_000_000
    assert seen["retry_routed_model"] == ROUTED_OPUS


def test_unconfigured_failover_window_keeps_upstream_behavior(agent):
    agent._failover_context_length = None

    result, compress, seen = _run(agent, 400_000)

    compress.assert_not_called()
    assert result["completed"] is True
    assert seen["retry_context_length"] == 1_000_000
