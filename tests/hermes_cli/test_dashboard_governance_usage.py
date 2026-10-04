from __future__ import annotations

from hermes_cli.dashboard_governance.context import DashboardGovernanceContext
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from hermes_cli.dashboard_governance.usage import check_usage_caps, record_tool_usage, read_usage_state


def _ctx(caps: dict) -> DashboardGovernanceContext:
    access = EffectiveAccess(
        subject=GovernanceSubject(email="operator@example.test"),
        mode="enforce",
        grants=GrantSet(usage_caps=caps),
    )
    return DashboardGovernanceContext(
        subject=access.subject,
        access=access,
        active_profile="default",
        session_id="session-1",
        request_id="request-1",
    )


def test_monthly_tool_call_cap_blocks_after_recorded_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = _ctx({"monthly_tool_calls": 1})

    assert check_usage_caps(ctx, "web_search").allowed is True
    record_tool_usage(ctx, "web_search")

    decision = check_usage_caps(ctx, "web_search")
    assert decision.allowed is False
    assert decision.reason == "monthly_tool_calls_exceeded"


def test_mcp_call_cap_counts_mcp_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = _ctx({"daily_mcp_calls": 1})

    assert check_usage_caps(ctx, "mcp_github_list_issues").allowed is True
    record_tool_usage(ctx, "mcp_github_list_issues")

    decision = check_usage_caps(ctx, "mcp_github_get_issue")
    assert decision.allowed is False
    assert decision.reason == "daily_mcp_calls_exceeded"


def test_background_process_cap_counts_background_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = _ctx({"daily_background_processes": 1})

    assert check_usage_caps(ctx, "terminal", {"background": True}).allowed is True
    record_tool_usage(ctx, "terminal", {"background": True})

    decision = check_usage_caps(ctx, "terminal", {"background": True})
    assert decision.allowed is False
    assert decision.reason == "daily_background_processes_exceeded"


def test_usage_state_uses_hashed_subject_not_raw_email(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = _ctx({"daily_tool_calls": 10})

    record_tool_usage(ctx, "web_search")
    state = read_usage_state()

    assert "operator@example.test" not in str(state)
    day_bucket = next(iter(state["days"].values()))
    counters = next(iter(day_bucket.values()))
    assert counters["tool_calls"] == 1
