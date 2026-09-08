"""An async continuation cannot expand its initiating principal's envelope."""
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest
import yaml

from hermes_cli.dashboard_governance.context import (
    DashboardGovernanceContext, context_from_env_payload, governance_context,
    policy_contexts, serialize_context_for_env,
)
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from hermes_cli.dashboard_governance.tool_policy import tool_allowed_for_context, tool_arguments_allowed_for_context
from hermes_cli.dashboard_governance.model_policy import model_allowed_for_context


class Registry:
    def get_entry(self, name): return SimpleNamespace(toolset="files", schema={})
    def get_toolset_for_tool(self, name): return "files"


def context(*, deny=None, grants=None, mode="enforce", roles=("member",), level="elevated"):
    subject = GovernanceSubject(email="alice@example.test", groups=("original-sso",))
    grants = grants or GrantSet(tools=frozenset({"*"}), toolsets=frozenset({"*"}),
        model_providers=frozenset({"*"}), models=frozenset({"*"}),
        file_read_roots=frozenset({"*"}), file_write_roots=frozenset({"*"}),
        cli_commands=frozenset({"*"}), cli_workdir_roots=frozenset({"*"}))
    access = EffectiveAccess(subject=subject, mode=mode, roles=frozenset(roles),
        permissions=frozenset({"*"}), profiles=frozenset({"*"}), grants=grants,
        deny=deny or GrantSet(), access_level=level, access_mode="blacklist")
    return DashboardGovernanceContext(subject=subject, access=access, active_profile="default", session_id="parent")


def continuation(original, current=None):
    current = current or context(roles=("admin",), level="admin")
    return replace(current, continuation_contexts=(serialize_context_for_env(original),))


@pytest.mark.parametrize("mode", ["enforce", "off", "report_only"])
def test_original_sso_deny_survives_current_role_promotion_and_mode_change(mode):
    original = context(deny=GrantSet(tools=frozenset({"web_search"})))
    resumed = continuation(original, context(mode=mode, roles=("admin",), level="admin"))
    assert not tool_allowed_for_context(resumed, "web_search", Registry()).allowed
    assert tool_allowed_for_context(resumed, "read_file", Registry()).allowed


def test_original_user_role_cannot_gain_elevated_terminal():
    original = context(level="user")
    assert not tool_allowed_for_context(continuation(original), "terminal", Registry()).allowed


def test_whitelist_and_current_denies_both_remain_hard_ceilings():
    original = context(grants=GrantSet(tools=frozenset({"read_file"}), file_read_roots=frozenset({"*"})))
    resumed = continuation(original)
    assert not tool_allowed_for_context(resumed, "write_file", Registry()).allowed
    assert tool_allowed_for_context(resumed, "read_file", Registry()).allowed
    current = context(deny=GrantSet(tools=frozenset({"read_file"})))
    assert not tool_allowed_for_context(continuation(original, current), "read_file", Registry()).allowed


def test_original_file_model_and_host_execution_restrictions_survive_roundtrip(tmp_path):
    secret = str(tmp_path / "private")
    original = context(deny=GrantSet(file_read_roots=frozenset({secret}), models=frozenset({"private-model"})))
    resumed = context_from_env_payload(serialize_context_for_env(continuation(original)))
    assert not tool_arguments_allowed_for_context(resumed, "read_file", {"path": secret + "/key.txt"}).allowed
    assert not tool_allowed_for_context(resumed, "terminal", Registry()).allowed
    assert not model_allowed_for_context(resumed, provider="openai", model="private-model").allowed
    assert model_allowed_for_context(resumed, provider="openai", model="allowed-model").allowed


def test_original_bot_ceiling_and_current_live_revoke_are_both_checked():
    original = context()
    bot = replace(original.access, grants=GrantSet(tools=frozenset({"read_file"}), file_read_roots=frozenset({"*"})))
    original = replace(original, bot_access_ceiling=bot, bot_access_check=lambda: True)
    resumed = replace(continuation(original), bot_access_ceiling=context().access, bot_access_check=lambda: True)
    assert tool_allowed_for_context(resumed, "read_file", Registry()).allowed
    assert not tool_allowed_for_context(resumed, "write_file", Registry()).allowed
    assert not tool_allowed_for_context(replace(resumed, bot_access_check=lambda: False), "read_file", Registry()).allowed


@pytest.mark.parametrize("change", ["subject", "profile", "malformed"])
def test_malformed_or_cross_principal_snapshots_fail_closed(change):
    original = context()
    if change == "subject": original = replace(original, subject=GovernanceSubject(email="other@example.test"), access=replace(original.access, subject=GovernanceSubject(email="other@example.test")))
    elif change == "profile": original = replace(original, active_profile="other-bot")
    resumed = continuation(original)
    if change == "malformed": resumed = replace(resumed, continuation_contexts=("invalid",))
    assert not tool_allowed_for_context(resumed, "read_file", Registry()).allowed
    assert not model_allowed_for_context(resumed, provider="openai", model="any").allowed


@pytest.mark.parametrize("mode", ["enforce", "off"])
def test_original_usage_limit_is_not_reset_by_continuation(tmp_path, monkeypatch, mode):
    from hermes_cli.dashboard_governance import usage
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    original = context(grants=replace(context().access.grants, usage_caps={"daily_tool_calls": 1}))
    resumed = continuation(original, context(mode=mode))
    assert usage.check_usage_caps(resumed, "read_file").allowed
    assert not usage.check_usage_caps(resumed, "read_file").allowed


@pytest.mark.parametrize("mode", ["enforce", "off"])
def test_original_env_allowlist_and_dwd_identity_do_not_expand(monkeypatch, mode):
    from tools.environments import local
    original = context(grants=replace(context().access.grants, env_vars=frozenset({"OLD_ALLOWED"})))
    current = context(mode=mode, roles=("admin",), grants=replace(context().access.grants, env_vars=frozenset({"OLD_ALLOWED", "NEW_SECRET"})))
    monkeypatch.setattr(local, "_hermes_dotenv_values", lambda names: {name: "fixture" for name in names})
    env = {}
    with governance_context(continuation(original, current)):
        local._inject_dwd_identity_env(env)
        local._inject_granted_env_vars(env)
    assert env == {"HERMES_DWD_IDENTITY": "alice@example.test", "OLD_ALLOWED": "fixture"}


def test_fresh_action_review_cannot_remove_original_deny(tmp_path, monkeypatch):
    from hermes_cli.dashboard_governance import action_approval
    policy = {"mode": "enforce", "roles": {"admin": {"grants": {
        "permissions": ["*"], "profiles": ["*"], "tools": {"builtins": ["*"]}}}},
        "users": {"alice@example.test": {"roles": ["admin"], "access_level": "admin", "access_mode": "blacklist"}}}
    path = tmp_path / "policy.yaml"; path.write_text(yaml.safe_dump(policy))
    original = context(deny=GrantSet(tools=frozenset({"web_search"})))
    resumed = replace(continuation(original), approval_policy_path=str(path))
    monkeypatch.setattr(action_approval, "_audit", lambda *a: None)
    fresh, _ = action_approval._fresh(resumed)
    assert fresh.continuation_contexts == resumed.continuation_contexts
    assert action_approval.authorize_tool_action(resumed, "web_search", {}, Registry())["approved"] is False


def test_actual_tool_dispatch_stays_blocked_before_handler(monkeypatch):
    import model_tools
    original = context(deny=GrantSet(tools=frozenset({"read_file"})))
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda *a, **k: pytest.fail("denied handler executed"))
    with governance_context(continuation(original)):
        result = model_tools.handle_function_call("read_file", {"path": "/private/file"},
                    skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True)
    assert "error" in json.loads(result)


def test_nested_continuations_keep_all_original_bounds_and_cache_separation():
    first = context(deny=GrantSet(tools=frozenset({"read_file"})))
    second = continuation(first, context(deny=GrantSet(tools=frozenset({"write_file"}))))
    third = continuation(second)
    assert len(policy_contexts(third)) == 3
    assert not tool_allowed_for_context(third, "read_file", Registry()).allowed
    assert not tool_allowed_for_context(third, "write_file", Registry()).allowed
    assert third.cache_fingerprint() != continuation(context()).cache_fingerprint()


@pytest.mark.parametrize("mode", ["enforce", "off", "report_only"])
def test_live_workspace_revoke_blocks_tools_models_and_candidate_roots(tmp_path, mode):
    root, private, shared = (str(tmp_path / name) for name in ("root", "private", "shared"))
    revoked = set()
    def check(path):
        return path not in revoked and not path.startswith(private)
    ctx = replace(context(mode=mode), workspace_path=root, workspace_access_check=check)
    assert tool_allowed_for_context(ctx, "read_file", Registry()).allowed
    assert tool_arguments_allowed_for_context(ctx, "read_file", {"path": shared + "/ok"}).allowed
    assert not tool_arguments_allowed_for_context(ctx, "read_file", {"path": private + "/secret"}).allowed
    assert not tool_arguments_allowed_for_context(ctx, "terminal", {"workdir": private}).allowed
    revoked.add(root)
    assert not tool_allowed_for_context(ctx, "web_search", Registry()).allowed
    assert not model_allowed_for_context(ctx, provider="openai", model="any").allowed


@pytest.mark.parametrize("mode", ["enforce", "off"])
def test_workspace_callback_does_not_serialize_and_original_workspace_stays_live(tmp_path, mode):
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    original = replace(context(mode=mode), workspace_path=first, workspace_access_check=lambda _: True)
    child = context_from_env_payload(serialize_context_for_env(original))
    assert child.workspace_path == first and child.workspace_access_check is None
    assert not tool_allowed_for_context(child, "read_file", Registry()).allowed
    current = replace(context(), workspace_path=first, workspace_access_check=lambda path: path != first)
    assert not tool_allowed_for_context(continuation(original, current), "read_file", Registry()).allowed
    permitted = replace(current, workspace_access_check=lambda _: True)
    assert tool_allowed_for_context(continuation(original, permitted), "read_file", Registry()).allowed
    relocated = replace(permitted, workspace_path=second)
    assert not tool_allowed_for_context(continuation(original, relocated), "read_file", Registry()).allowed
    assert not model_allowed_for_context(continuation(original, relocated), provider="openai", model="any").allowed


@pytest.mark.parametrize("entry", ["direct_api_call", "interruptible_api_call", "interruptible_streaming_api_call"])
def test_live_workspace_revoke_precedes_each_actual_model_request_entry(entry):
    from agent import chat_completion_helpers
    # Deliberately has no client/runtime attributes: a denied request must not
    # reach client creation, activity threads, or any provider-specific branch.
    agent = SimpleNamespace(provider="openai", model="any")
    ctx = replace(context(mode="off"), workspace_path="/revoked", workspace_access_check=lambda _: False)
    with governance_context(ctx), pytest.raises(PermissionError, match="workspace_access_revoked"):
        getattr(chat_completion_helpers, entry)(agent, {})


@pytest.mark.parametrize("mode", ["off", "report_only"])
def test_retained_inactive_permissions_do_not_become_enforced_for_live_workspace(mode):
    original = replace(context(mode=mode), workspace_path="/original", workspace_access_check=lambda _: True)
    original = replace(original, access=replace(original.access, permissions=frozenset(), profiles=frozenset()))
    current = replace(context(), workspace_path="/original", workspace_access_check=lambda _: True)
    resumed = continuation(original, current)
    assert tool_allowed_for_context(resumed, "read_file", Registry()).allowed
    assert not tool_allowed_for_context(replace(resumed, workspace_access_check=lambda path: path != "/original"), "read_file", Registry()).allowed
