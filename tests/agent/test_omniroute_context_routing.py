"""Runtime context-window updates from OmniRoute response headers.

The OmniRoute router advertises ``synthwave-auto`` at 200k but may route to
``claude-fable-5`` (1M) or ``gpt-5.6-sol`` (400k).  This module detects the
actual routed model from the API response and updates
``ContextCompressor`` so compression happens at the right threshold.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.chat_completion_helpers import capture_omniroute_route


# ── helpers ──


class _FakeHeaders(dict):
    """Case-insensitive header mapping for test fixtures."""

    def get(self, key, default=None):
        key = str(key).lower()
        for current, value in self.items():
            if str(current).lower() == key:
                return value
        return default


class _FakeHttpResponse:
    """Mimics ``httpx.Response`` enough for ``_extract_omniroute_route``."""

    def __init__(self, headers: dict):
        self.headers = _FakeHeaders(headers)


def _agent():
    compressor = MagicMock()
    compressor.model = "synthwave-auto"
    compressor.context_length = 200_000
    return SimpleNamespace(
        model="synthwave-auto",
        provider="custom:omniroute",
        base_url="http://127.0.0.1:20128/v1",
        api_key="test-key",
        api_mode="chat_completions",
        max_tokens=None,
        context_compressor=compressor,
    )


# ── capture_omniroute_route tests ──


def test_from_agent_route_info(monkeypatch):
    """Header-based route info on the agent takes priority over response.model."""
    agent = _agent()
    agent._omniroute_route_info = ("cc", "claude-fable-5")
    response = SimpleNamespace(model="different-model")

    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda model, **kwargs: 1_000_000,
    )

    capture_omniroute_route(agent, response)

    # Should use the provider-qualified slug, not response.model
    agent.context_compressor.update_model.assert_called_once_with(
        model="cc/claude-fable-5",
        context_length=1_000_000,
        base_url="http://127.0.0.1:20128/v1",
        api_key="test-key",
        provider="custom:omniroute",
        api_mode="chat_completions",
        max_tokens=None,
    )
    assert agent._effective_routed_model == "cc/claude-fable-5"
    assert agent._effective_context_length == 1_000_000


def test_fallback_to_response_model(monkeypatch):
    """When no route info on agent, use response.model."""
    agent = _agent()
    response = SimpleNamespace(model="claude-fable-5")

    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda model, **kwargs: 1_048_576,
    )

    capture_omniroute_route(agent, response)

    agent.context_compressor.update_model.assert_called_once()
    assert agent._effective_routed_model == "claude-fable-5"
    assert agent._effective_context_length == 1_048_576


def test_skips_when_model_matches(monkeypatch):
    """If resolved model matches agent.model, skip (normal endpoint)."""
    agent = _agent()
    response = SimpleNamespace(model="synthwave-auto")
    resolver = MagicMock()
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length", resolver,
    )

    capture_omniroute_route(agent, response)

    resolver.assert_not_called()
    agent.context_compressor.update_model.assert_not_called()


def test_skips_when_route_info_matches(monkeypatch):
    """Same route info twice in same session — no redundant update."""
    agent = _agent()
    agent._omniroute_route_info = ("cc", "claude-fable-5")
    agent._effective_routed_model = "cc/claude-fable-5"
    agent._effective_context_length = 1_000_000
    response = SimpleNamespace(model="claude-fable-5")
    resolver = MagicMock()
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length", resolver,
    )

    capture_omniroute_route(agent, response)

    resolver.assert_not_called()
    agent.context_compressor.update_model.assert_not_called()


def test_route_info_beats_agent_model_check(monkeypatch):
    """Route info differs even when response.model == agent.model."""
    agent = _agent()
    agent._omniroute_route_info = ("cc", "claude-opus-4-8")
    response = SimpleNamespace(model="synthwave-auto")

    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda model, **kwargs: 1_000_000,
    )

    capture_omniroute_route(agent, response)

    agent.context_compressor.update_model.assert_called_once_with(
        model="cc/claude-opus-4-8", context_length=1_000_000, **{
            "base_url": "http://127.0.0.1:20128/v1",
            "api_key": "test-key",
            "provider": "custom:omniroute",
            "api_mode": "chat_completions",
            "max_tokens": None,
        },
    )


def test_fails_open_on_metadata_error(monkeypatch):
    """If the metadata lookup throws, skip — never crash the conversation."""
    agent = _agent()
    agent._omniroute_route_info = ("cc", "claude-fable-5")
    response = SimpleNamespace(model="claude-fable-5")
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        MagicMock(side_effect=RuntimeError("metadata unavailable")),
    )

    capture_omniroute_route(agent, response)

    agent.context_compressor.update_model.assert_not_called()
    assert not hasattr(agent, "_effective_routed_model")


def test_route_switch_updates_compressor(monkeypatch):
    """Mid-session route switch replaces the live compressor threshold."""
    agent = _agent()
    agent._omniroute_route_info = ("cc", "claude-fable-5")
    agent._effective_routed_model = "cc/claude-fable-5"
    agent._effective_context_length = 1_000_000
    # New route comes in
    agent._omniroute_route_info = ("codex", "gpt-5.6-sol")
    response = SimpleNamespace(model="gpt-5.6-sol")

    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda model, **kwargs: 400_000,
    )

    capture_omniroute_route(agent, response)

    agent.context_compressor.update_model.assert_called_once()
    assert agent._effective_routed_model == "codex/gpt-5.6-sol"
    assert agent._effective_context_length == 400_000
