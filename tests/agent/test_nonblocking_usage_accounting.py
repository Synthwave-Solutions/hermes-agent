"""Response accounting uses fresh cached metadata without network waits."""
import time
from unittest.mock import Mock
from agent import model_metadata as metadata
from agent.usage_pricing import estimate_usage_cost, normalize_usage


def test_cold_custom_accounting_does_not_request_metadata(monkeypatch):
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache_time", {})
    metadata._ensure_requests()
    request = Mock(side_effect=AssertionError("no HTTP on response accounting"))
    monkeypatch.setattr(metadata.requests, "get", request)
    result = estimate_usage_cost("codex/gpt-6-astra", normalize_usage({"prompt_tokens": 55812, "completion_tokens": 10}), provider="custom", base_url="http://127.0.0.1:20128/v1", cached_only=True)
    assert result.amount_usd is None
    assert result.status == "unknown"
    request.assert_not_called()


def test_cached_endpoint_metadata_stays_available_without_network(monkeypatch):
    url = "http://127.0.0.1:20128/v1"
    payload = {"codex/gpt-6-astra": {"id": "codex/gpt-6-astra", "pricing": {"prompt": "0.000001", "completion": "0.000002"}}}
    key = metadata._normalize_base_url(url)
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache", {key: payload})
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache_time", {key: time.time()})
    assert metadata.fetch_endpoint_model_metadata(url, cached_only=True) == payload
    result = estimate_usage_cost("codex/gpt-6-astra", normalize_usage({"prompt_tokens": 100, "completion_tokens": 10}), provider="custom", base_url=url, cached_only=True)
    assert result.amount_usd is not None
    assert result.amount_usd > 0


def test_expired_endpoint_cache_is_unknown_without_refresh(monkeypatch):
    url = "http://127.0.0.1:20128/v1"
    key = metadata._normalize_base_url(url)
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache", {key: {"old": {}}})
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache_time", {key: 0})
    assert metadata.fetch_endpoint_model_metadata(url, cached_only=True) == {}


def test_real_conversation_completion_uses_cache_only_accounting(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from run_agent import AIAgent
    from agent import conversation_loop
    from agent.usage_pricing import estimate_usage_cost as actual_estimate
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  context_length: 256000\n")
    agent = AIAgent(api_key="test", base_url="http://127.0.0.1:20128/v1", model="codex/gpt-6-astra", provider="custom", quiet_mode=True, skip_context_files=True, skip_memory=True, skip_background_review=True, enabled_toolsets=[], max_iterations=2)
    response = SimpleNamespace(model=agent.model, choices=[SimpleNamespace(message=SimpleNamespace(content="QA_OK", tool_calls=[], reasoning_content=None), finish_reason="stop")], usage=SimpleNamespace(prompt_tokens=100, completion_tokens=2, total_tokens=102))
    monkeypatch.setattr(agent, "_interruptible_api_call", lambda *a, **k: response)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", lambda *a, **k: response)
    calls = []
    def checked_estimate(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs.get("cached_only") is True
        return actual_estimate(*args, **kwargs)
    monkeypatch.setattr(conversation_loop, "estimate_usage_cost", checked_estimate)
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache", {})
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_cache_time", {})
    metadata._ensure_requests()
    monkeypatch.setattr(metadata.requests, "get", Mock(side_effect=AssertionError("completion must not probe metadata")))
    result = agent.run_conversation("Reply QA_OK without tools")
    assert result["final_response"] == "QA_OK"
    assert len(calls) == 1
    assert agent.session_cost_status == "unknown"
