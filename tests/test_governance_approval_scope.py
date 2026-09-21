"""14-09-2026: approval: {mode, prompt} governs the access-request queue, not
every tool call. Only cli.approval_commands still force a per-call human review."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hermes_cli.dashboard_governance import action_approval as review
from hermes_cli.dashboard_governance import context as governance_context
from hermes_cli.dashboard_governance.context import DashboardGovernanceContext


@pytest.fixture(autouse=True)
def _unbind_after_each_test():
    # authorize_tool_action binds the reviewed context for the dispatch that
    # follows; here nothing follows, so leave no binding behind for other tests.
    yield
    governance_context._current_governance_context.set(None)
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet


def _ctx(approval=()):
    access = EffectiveAccess(
        subject=GovernanceSubject(email="iflair@synthwave.solutions"), mode="enforce",
        access_mode="blacklist", access_level="elevated",
        approval_mode="automatic", approval_prompt="approve ordinary technical work", approval_configured=True,
        grants=GrantSet(cli_commands=frozenset({"*"}), cli_approval_commands=frozenset(approval)),
    )
    return DashboardGovernanceContext(subject=access.subject, access=access, session_id="s1", approval_policy_path="/x")


def test_a_configured_approval_section_no_longer_reviews_ordinary_tool_calls(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr(review, "_fresh", lambda c: (c, "rev"))
    monkeypatch.setattr(review, "_hard_check", lambda c, t, a, r: "")
    monkeypatch.setattr(review, "_ask_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model must not be asked")))
    for tool, args in (("read_file", {"path": "/workspace/x"}), ("terminal", {"command": "ls -la"}), ("todo", {})):
        assert review.authorize_tool_action(ctx, tool, args, None) is None


def test_a_hard_denial_still_refuses(monkeypatch):
    ctx = _ctx()
    monkeypatch.setattr(review, "_fresh", lambda c: (c, "rev"))
    monkeypatch.setattr(review, "_hard_check", lambda c, t, a, r: "cli_command_denied")
    monkeypatch.setattr(review, "_audit", lambda *a, **k: None)
    result = review.authorize_tool_action(ctx, "terminal", {"command": "sudo ls"}, None)
    assert result == {"approved": False, "source": "policy", "reason": "cli_command_denied"}


def test_mandatory_cli_review_still_parks_on_a_human(monkeypatch):
    ctx = _ctx(approval={"deploy-prod"})
    monkeypatch.setattr(review, "_fresh", lambda c: (c, "rev"))
    monkeypatch.setattr(review, "_hard_check", lambda c, t, a, r: "")
    monkeypatch.setattr(review, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(review, "_ask_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model must not be asked")))
    import tools.approval as approval
    monkeypatch.setattr(approval, "request_governance_action_approval",
                        lambda tool, serialized, fallback, op: {"approved": False, "message": "declined"})
    result = review.authorize_tool_action(ctx, "terminal", {"command": "deploy-prod api"}, None)
    assert result["approved"] is False and result["source"] == "manual"
