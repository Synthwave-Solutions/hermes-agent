from dataclasses import replace
import hashlib
from hermes_cli.dashboard_governance.context import (
    DashboardGovernanceContext, serialize_context_for_env, context_from_env_payload,
    bind_governance_context, reset_governance_context)
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from hermes_cli.dashboard_governance.resolver import _wildcard_grants
from hermes_cli.dashboard_governance.tool_policy import tool_arguments_allowed_for_context
from hermes_cli.dashboard_governance import grant_requests


def context():
    subject = GovernanceSubject(email="person@example.test")
    ceiling = EffectiveAccess(subject=subject, mode="enforce", profiles=frozenset({"private-bot"}),
        grants=replace(_wildcard_grants(), cli_commands=frozenset({"git"})))
    return DashboardGovernanceContext(subject=subject,
        access=EffectiveAccess(subject=subject, mode="enforce", grants=_wildcard_grants()),
        active_profile="private-bot", session_id="session1", request_id="run1",
        user_message_sha256=hashlib.sha256(b"ask").hexdigest(),
        bot_access_ceiling=ceiling, bot_access_check=lambda: True)


def test_fresh_human_grants_cannot_widen_ceiling_and_project_exception_cannot_override():
    ctx = context()
    assert tool_arguments_allowed_for_context(ctx, "terminal", {"command":"git status"}).allowed
    assert not tool_arguments_allowed_for_context(ctx, "terminal", {"command":"python script.py"}).allowed
    revoked = replace(ctx, bot_access_check=lambda: False)
    assert not tool_arguments_allowed_for_context(revoked, "read_file", {"path":"/tmp/file"}).allowed
    restored = context_from_env_payload(serialize_context_for_env(ctx))
    assert restored.bot_access_ceiling.grants.cli_commands == frozenset({"git"})
    assert restored.subject == ctx.subject
    # A child transport without the fresh ACL callback fails closed.
    assert not tool_arguments_allowed_for_context(restored, "terminal", {"command":"git status"}).allowed
    assert context().cache_fingerprint() != replace(ctx, bot_access_ceiling=None).cache_fingerprint()


def test_real_approval_recheck_retains_bot_ceiling(tmp_path, monkeypatch):
    import model_tools
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path / "spool"))
    policy = tmp_path / "policy.yaml"
    policy.write_text('mode: enforce\nusers:\n  person@example.test:\n    grants:\n      profiles: [default]\n      tools:\n        builtins: ["*"]\n      cli:\n        commands: ["*"]\n')
    ctx = replace(context(), approval_waiter=lambda op: True, approval_policy_path=str(policy))
    token = bind_governance_context(ctx)
    try:
        for call, command, detail, expected in (
            ("git-call", "git status", "git", True),
            ("python-call", "python script.py", "python", False)):
            grant_requests.record_denial(ctx, "terminal", "cli_command_not_allowed", detail,
                tool_call_id=call, dispatch_session_id="session1")
            assert model_tools._wait_for_governance_grant(ctx, "terminal", {"command":command},
                "cli_command_not_allowed", detail, call, "session1") is expected
    finally:
        reset_governance_context(token)


def test_empty_cli_and_unselected_skill_are_denied():
    ctx = context()
    ctx = replace(ctx, bot_access_ceiling=replace(ctx.bot_access_ceiling,
        grants=replace(ctx.bot_access_ceiling.grants, cli_commands=frozenset(),
                       skills_load=frozenset({"selected"}))))
    assert not tool_arguments_allowed_for_context(ctx, "terminal", {"command":"git status"}).allowed
    assert not tool_arguments_allowed_for_context(ctx, "skill_view", {"name":"other"}).allowed
    assert tool_arguments_allowed_for_context(ctx, "skill_view", {"name":"selected"}).allowed
