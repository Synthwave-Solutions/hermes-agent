from __future__ import annotations

from unittest.mock import patch

import pytest

from hermes_cli.dashboard_governance.context import (
    DashboardGovernanceContext,
    serialize_context_for_env,
)
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from run_agent import AIAgent


def _tool_defs():
    return []


def _env_payload(*, providers, models) -> str:
    subject = GovernanceSubject(email="stub@example.test")
    access = EffectiveAccess(
        subject=subject,
        mode="enforce",
        grants=GrantSet(
            model_providers=frozenset(providers),
            models=frozenset(models),
        ),
    )
    ctx = DashboardGovernanceContext(
        subject=subject,
        access=access,
    )
    return serialize_context_for_env(ctx)


def test_aiagent_init_blocks_forbidden_dashboard_governance_model(monkeypatch):
    monkeypatch.setenv(
        "HERMES_DASHBOARD_GOVERNANCE_CONTEXT",
        _env_payload(providers=["openrouter"], models=["anthropic/claude-sonnet-4.6"]),
    )

    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        pytest.raises(PermissionError, match="model_provider_not_allowed"),
    ):
        AIAgent(
            api_key="test-key",
            base_url="https://api.anthropic.com",
            provider="anthropic",
            model="claude-opus-4-6",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )


def test_aiagent_init_allows_granted_dashboard_governance_model(monkeypatch):
    monkeypatch.setenv(
        "HERMES_DASHBOARD_GOVERNANCE_CONTEXT",
        _env_payload(providers=["openrouter"], models=["anthropic/claude-sonnet-4.6"]),
    )

    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="anthropic/claude-sonnet-4.6",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent.provider == "openrouter"
    assert agent.model == "anthropic/claude-sonnet-4.6"
