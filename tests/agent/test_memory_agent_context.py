"""Regression for #80646: the ``agent_context`` handed to memory providers follows the platform
(``cron`` / ``subagent`` skip writes per the ``MemoryProvider.initialize`` contract) instead of a
hardcoded ``"primary"`` that let cron turns land in stores configured to skip them.
"""

from types import SimpleNamespace

import pytest

from agent.agent_init import _GATEWAY_IDENTITY_PARAMS, _memory_provider_init_kwargs


def _fake_agent():
    """The attribute surface ``_memory_provider_init_kwargs`` reads."""
    return SimpleNamespace(
        session_id="sess-80646", _session_db=None, _emit_warning=None, _emit_status=None,
        session_cwd=None, **{f"_{name}": None for name in _GATEWAY_IDENTITY_PARAMS},
    )


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("cron", "cron"), ("subagent", "subagent"), ("telegram", "primary"), (None, "primary")],
)
def test_agent_context_follows_the_platform(platform, expected):
    assert _memory_provider_init_kwargs(_fake_agent(), platform)["agent_context"] == expected


def _keyed_agent(session_key):
    agent = _fake_agent()
    agent._gateway_session_key = session_key
    return agent


def test_unscoped_api_server_call_is_background():
    """Scripts posting a full prompt to the API server without X-Hermes-Session-Key must not
    land in long-term memory as [USER] turns."""
    assert _memory_provider_init_kwargs(_keyed_agent(None), "api_server")["agent_context"] == "background"
    assert _memory_provider_init_kwargs(_keyed_agent(""), "api_server")["agent_context"] == "background"


def test_api_server_call_with_session_key_stays_primary():
    kwargs = _memory_provider_init_kwargs(_keyed_agent("owui:someone@example.com"), "api_server")
    assert kwargs["agent_context"] == "primary"


def test_oneshot_cli_run_is_background(monkeypatch):
    """``hermes -z`` / ``hermes chat -q`` (session source "oneshot") are script-driven."""
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE_EXPLICIT", raising=False)
    assert _memory_provider_init_kwargs(_fake_agent(), "cli")["agent_context"] == "background"


def test_interactive_cli_stays_primary(monkeypatch):
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    assert _memory_provider_init_kwargs(_fake_agent(), "cli")["agent_context"] == "primary"


def test_mnemosyne_skips_the_background_context():
    """The default Mnemosyne skip list must cover the context this module hands out."""
    mnemosyne_hermes = pytest.importorskip("mnemosyne_hermes")
    provider = mnemosyne_hermes.MnemosyneMemoryProvider()
    assert "background" in provider._skip_contexts
