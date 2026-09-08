"""Real policy, serialization, gateway queue and final tool dispatch coverage."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.dashboard_governance import action_approval as review
from hermes_cli.dashboard_governance.context import (
    DashboardGovernanceContext, governance_context, serialize_context_for_env, context_from_env_payload,
    bind_governance_context, reset_governance_context,
)
from hermes_cli.dashboard_governance.loader import load_governance_policy
from hermes_cli.dashboard_governance.models import GovernanceSubject
from hermes_cli.dashboard_governance.resolver import resolve_effective_access
from hermes_cli.dashboard_governance.tool_policy import decide_tool_access, decide_tool_argument_access


class Registry:
    def get_entry(self, name):
        return SimpleNamespace(toolset="web", schema={})

    def get_toolset_for_tool(self, name):
        return "web"


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    token = bind_governance_context(None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DASHBOARD_GOVERNANCE_CONTEXT", raising=False)
    policy = {
        "mode": "enforce", "roles": {"member": {"grants": {
            "permissions": ["chat:use"], "profiles": ["alice"], "routes": ["*"],
            "tools": {"builtins": ["*"]}, "files": {"read_roots": [str(tmp_path)]},
        }}},
        "users": {"alice@example.test": {"roles": ["member"], "access_level": "user",
            "access_mode": "blacklist", "approval": {"mode": "automatic", "prompt": "Allow public research. Deny sharing private information."}}},
    }
    path = tmp_path / "dashboard-governance.yaml"
    def write():
        path.write_text(yaml.safe_dump(policy))
    write()
    subject = GovernanceSubject(email="alice@example.test")
    def context():
        loaded = load_governance_policy(path=path)
        return DashboardGovernanceContext(subject=subject, access=resolve_effective_access(loaded, subject),
                                          active_profile="alice", session_id="alice-session", request_id="run-one",
                                          approval_policy_path=str(path))
    monkeypatch.setattr(review, "_audit", lambda *args: None)
    yield policy, write, context, tmp_path
    reset_governance_context(token)


def model(monkeypatch, decision="approve", confidence=.99, on_call=None):
    from agent import auxiliary_client
    calls = []
    def call(**kwargs):
        calls.append(kwargs)
        if on_call:
            on_call()
        return {"choices": [{"message": {"content": json.dumps({"decision": decision, "reason": "The action matches the administrator rules.", "confidence": confidence})}}]}
    monkeypatch.setattr(auxiliary_client, "call_llm", call)
    return calls


@pytest.mark.parametrize("decision,allowed", [("approve", True), ("deny", False)])
def test_automatic_real_adapter_and_scoped_verdict(fixture, monkeypatch, decision, allowed):
    _, _, context, _ = fixture
    calls = model(monkeypatch, decision)
    result = review.authorize_tool_action(context(), "web_search", {"query": "public facts", "api_key": "secret-value"}, Registry())
    assert result["approved"] is allowed
    assert result["source"] == "automatic"
    assert len(calls) == 1
    assert "secret-value" not in json.dumps(calls)
    assert calls[0]["timeout"] == 20


@pytest.mark.parametrize("mutation", ["deny", "whitelist", "profile"])
def test_hard_ceiling_before_any_model_call(fixture, monkeypatch, mutation):
    policy, write, context, _ = fixture
    user = policy["users"]["alice@example.test"]
    if mutation == "deny":
        user["deny"] = {"tools": {"builtins": ["web_search"]}}
    elif mutation == "whitelist":
        user["access_mode"] = "whitelist"
    else:
        policy["roles"]["member"]["grants"]["profiles"] = ["bob"]
    write()
    calls = model(monkeypatch)
    result = review.authorize_tool_action(context(), "web_search", {"query": "public facts"}, Registry())
    assert result["approved"] is False
    assert calls == []


def test_policy_changed_during_ai_review_does_not_execute(fixture, monkeypatch):
    policy, write, context, _ = fixture
    ctx = context()
    def revoke():
        policy["users"]["alice@example.test"]["deny"] = {"tools": {"builtins": ["web_search"]}}
        write()
    model(monkeypatch, on_call=revoke)
    result = review.authorize_tool_action(ctx, "web_search", {"query": "public"}, Registry())
    assert result["approved"] is False
    assert result["source"] == "policy"


@pytest.mark.parametrize("decision,confidence", [("manual", .99), ("approve", .2)])
def test_uncertain_model_uses_real_one_shot_gateway_queue(fixture, monkeypatch, decision, confidence):
    _, _, context, _ = fixture
    from tools import approval
    model(monkeypatch, decision, confidence)
    token = approval.set_current_session_key("alice-session")
    shown = []
    def respond(data):
        shown.append(data)
        approval.resolve_gateway_approval("alice-session", "once", request_id=data.get("request_id"))
    approval.register_gateway_notify("alice-session", respond)
    try:
        result = review.authorize_tool_action(context(), "web_search", {"query": "public"}, Registry())
    finally:
        approval.unregister_gateway_notify("alice-session")
        approval.reset_current_session_key(token)
    assert result["approved"] is True
    assert result["source"] == "manual"
    assert len(shown) == 1
    assert shown[0]["allow_session"] is False
    assert shown[0]["allow_permanent"] is False
    assert not approval.list_gateway_approvals("alice-session")


def test_provider_error_falls_back_to_manual_without_auto_consent(fixture, monkeypatch):
    _, _, context, _ = fixture
    from agent import auxiliary_client
    from tools import approval
    monkeypatch.setattr(auxiliary_client, "call_llm", lambda **kw: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(approval, "request_governance_action_approval", lambda *args: {"approved": False, "message": "Waiting for manual review"})
    result = review.authorize_tool_action(context(), "web_search", {}, Registry())
    assert result["approved"] is False
    assert result["source"] == "manual"


def test_context_roundtrip_retains_denies_and_modes(fixture):
    policy, write, context, _ = fixture
    policy["users"]["alice@example.test"]["deny"] = {"tools": {"builtins": ["web_search"]}}
    write()
    ctx = context()
    restored = context_from_env_payload(serialize_context_for_env(ctx))
    assert restored.access.approval_mode == "automatic"
    assert restored.access.approval_prompt == ctx.access.approval_prompt
    assert not decide_tool_access(restored.access, "web_search", Registry()).allowed
    assert ctx.cache_fingerprint() == restored.cache_fingerprint()


def test_file_deny_under_wildcard_and_empty_whitelist_roots(fixture):
    policy, write, context, home = fixture
    user = policy["users"]["alice@example.test"]
    user["deny"] = {"files": {"read_roots": [str(home / "private")]}}
    write()
    assert not decide_tool_argument_access(context().access, "read_file", {"path": str(home / "private/key")}).allowed
    assert decide_tool_argument_access(context().access, "read_file", {"path": str(home / "public")}).allowed


@pytest.mark.parametrize("reply", ['true', '{}', '[]', '{"decision":"approve","reason":"ok","confidence":true}', '{"decision":"approve","reason":"ok","confidence":NaN}', '```json\n{}\n```'])
def test_strict_model_contract(reply):
    assert review.parse_verdict(reply) is None


def test_legacy_user_keeps_current_approval_behavior(fixture, monkeypatch):
    policy, write, context, _ = fixture
    policy["users"]["alice@example.test"] = {"roles": ["member"]}
    write()
    calls = model(monkeypatch)
    assert review.authorize_tool_action(context(), "web_search", {}, Registry()) is None
    assert calls == []


def test_explicit_manual_setting_without_access_controls_is_not_legacy(fixture, monkeypatch):
    policy, write, context, _ = fixture
    from tools import approval
    policy["users"]["alice@example.test"] = {"roles": ["member"], "approval": {"mode": "manual", "prompt": ""}}
    write()
    asked = []
    monkeypatch.setattr(approval, "request_governance_action_approval", lambda *args: asked.append(args) or {"approved": False})
    result = review.authorize_tool_action(context(), "web_search", {}, Registry())
    assert result["approved"] is False
    assert len(asked) == 1


def test_delegated_context_keeps_authoritative_policy_and_rechecks_revocation(fixture, monkeypatch):
    policy, write, context, _ = fixture
    delegated = context_from_env_payload(serialize_context_for_env(context()))
    assert delegated.approval_policy_path == context().approval_policy_path
    calls = model(monkeypatch)
    assert review.authorize_tool_action(delegated, "web_search", {}, Registry())["approved"] is True
    policy["users"]["alice@example.test"]["deny"] = {"tools": {"builtins": ["web_search"]}}
    write()
    assert review.authorize_tool_action(delegated, "web_search", {}, Registry())["approved"] is False
    assert len(calls) == 1


def test_legacy_running_turn_adopts_new_deny_before_dispatch(fixture, monkeypatch):
    policy, write, context, _ = fixture
    policy["users"]["alice@example.test"] = {"roles": ["member"]}
    write()
    running = context()
    policy["users"]["alice@example.test"]["deny"] = {"tools": {"builtins": ["web_search"]}}
    write()
    calls = model(monkeypatch)
    assert review.authorize_tool_action(running, "web_search", {}, Registry())["approved"] is False
    assert not calls


def test_level_only_edit_preserves_legacy_approval_and_role_resources(fixture, monkeypatch):
    policy, write, context, _ = fixture
    from tools import approval
    policy["users"]["alice@example.test"] = {"roles": ["member"], "access_level": "elevated"}
    write()
    calls = model(monkeypatch)
    monkeypatch.setattr(approval, "request_governance_action_approval", lambda *args: pytest.fail("legacy approval must stay unchanged"))
    assert review.authorize_tool_action(context(), "web_search", {}, Registry()) is None
    assert not calls


def test_access_mode_only_uses_user_level_ceiling(fixture):
    policy, write, context, _ = fixture
    policy["users"]["alice@example.test"].pop("access_level")
    write()
    assert not decide_tool_access(context().access, "execute_code", Registry()).allowed
    assert not decide_tool_access(context().access, "terminal", Registry()).allowed


@pytest.mark.parametrize("denial", [{"files": {"read_roots": ["/private/restricted"]}}, {"env": {"vars": ["PRIVATE_TOKEN"]}}])
def test_host_execution_cannot_derive_paths_or_environment_behind_resource_deny(fixture, monkeypatch, denial):
    policy, write, context, _ = fixture
    user = policy["users"]["alice@example.test"]
    user.update(access_level="elevated", deny=denial)
    role = policy["roles"]["member"]["grants"]
    role["permissions"].append("terminal:use")
    role["cli"] = {"commands": ["*"], "workdir_roots": ["*"]}
    write()
    calls = model(monkeypatch)
    # No restricted literal appears in the executable command; argument
    # matching alone would allow this class of indirect access.
    command = "python -c 'import base64,os; print(os.environ); print(open(base64.b64decode(\"L3ByaXZhdGUvcmVzdHJpY3RlZA==\").decode()).read())'"
    for tool, args in (("terminal", {"command": command}), ("execute_code", {"code": command})):
        result = review.authorize_tool_action(context(), tool, args, Registry())
        assert result["approved"] is False
        assert result["reason"] == "host_execution_conflicts_with_resource_deny"
    assert calls == []


def test_audit_failure_prevents_approved_action_execution(fixture, monkeypatch):
    import model_tools
    _, _, context, _ = fixture
    model(monkeypatch)
    monkeypatch.setattr(review, "_audit", lambda *a: (_ for _ in ()).throw(OSError("audit unavailable")))
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda *a, **kw: pytest.fail("audit failure cannot authorize execution"))
    with governance_context(context()):
        result = model_tools.handle_function_call("web_search", {"query": "public"}, session_id="alice-session",
            skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True)
    assert "error" in json.loads(result)


@pytest.mark.parametrize("approval_mode", ["automatic", "manual"])
def test_real_delegated_worker_retains_bot_checker_and_parent_manual_interface(fixture, monkeypatch, approval_mode):
    import model_tools
    from tools import approval, delegate_tool
    from hermes_cli.dashboard_governance.context import current_governance_context
    from agent.delegation_context import is_delegated_child_context
    policy, write, context, _ = fixture
    policy["users"]["alice@example.test"]["approval"]["mode"] = approval_mode
    write()
    revoked = {"value": False}
    checker = lambda: not revoked["value"]
    parent_context = context()
    parent_context = replace(parent_context, active_profile="assigned-bot",
        bot_access_ceiling=parent_context.access, bot_access_check=checker)
    calls = model(monkeypatch)
    dispatched, shown = [], []
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda *a, **kw: dispatched.append(a) or '{"ok":true}')
    # Even a globally auto-approved subagent cannot bypass manual governance.
    monkeypatch.setattr(delegate_tool, "_get_subagent_approval_callback", lambda: delegate_tool._subagent_auto_approve)
    def respond(data):
        shown.append(data)
        approval.resolve_gateway_approval("alice-session", "once", request_id=data["request_id"])
    approval.register_gateway_notify("alice-session", respond)
    class Child:
        session_id = "child-session"
        _credential_pool = None
        _subagent_id = None
        _delegate_depth = 1
        _parent_subagent_id = None
        model = "test-model"
        session_prompt_tokens = 0
        session_completion_tokens = 0
        session_estimated_cost_usd = 0
        session_reasoning_tokens = 0
        def get_activity_summary(self):
            return {"api_call_count": 1, "max_iterations": 1, "current_tool": None}
        def run_conversation(self, **kwargs):
            assert is_delegated_child_context()
            assert current_governance_context().bot_access_check is checker
            result = model_tools.handle_function_call("web_search", {"query": "public"}, session_id=self.session_id,
                skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True)
            return {"final_response": result, "completed": True, "api_calls": 1, "messages": []}
        def close(self):
            pass
    parent = SimpleNamespace(_current_task_id=None, _delegate_depth=0, _touch_activity=lambda desc: None)
    try:
        with governance_context(parent_context):
            allowed = delegate_tool._run_single_child(0, "read public facts", Child(), parent)
        assert json.loads(allowed["summary"]).get("ok") is True
        revoked["value"] = True
        with governance_context(parent_context):
            blocked = delegate_tool._run_single_child(0, "read after revoke", Child(), parent)
        assert "error" in json.loads(blocked["summary"])
    finally:
        approval.unregister_gateway_notify("alice-session")
    assert len(dispatched) == 1
    assert len(calls) == (1 if approval_mode == "automatic" else 0)
    assert len(shown) == (1 if approval_mode == "manual" else 0)


def test_delegated_manual_without_parent_interface_cannot_use_autoapprove(monkeypatch):
    from tools import approval, delegate_tool
    from agent.delegation_context import delegated_child_context
    monkeypatch.setattr(approval, "_resolve_cli_approval_callback", lambda: delegate_tool._subagent_auto_approve)
    with delegated_child_context("unattended-child"):
        result = approval.request_governance_action_approval("write_file", "one write", "Manual review", "operation")
    assert result["approved"] is False


def test_bot_profile_acl_extends_profile_ceiling_but_never_overrides_deny(fixture):
    policy, write, context, _ = fixture
    ctx = context()
    ctx = replace(ctx, active_profile="assigned-bot", bot_access_ceiling=ctx.access, bot_access_check=lambda: True)
    fresh, _ = review._fresh(ctx)
    assert fresh.access.is_profile_allowed("assigned-bot")
    policy["users"]["alice@example.test"]["deny"] = {"profiles": ["assigned-bot"]}
    write()
    fresh, _ = review._fresh(ctx)
    assert not fresh.access.is_profile_allowed("assigned-bot")
    with pytest.raises(ValueError, match="bot_access_revoked"):
        review._fresh(replace(ctx, bot_access_check=lambda: False))


def test_unscoped_profile_search_filters_governed_denied_descendants(fixture, monkeypatch):
    from tools import file_tools
    policy, write, context, root = fixture
    private = str(root / "private.txt")
    allowed = str(root / "public.txt")
    policy["users"]["alice@example.test"]["deny"] = {"files": {"read_roots": [private]}}
    write()
    monkeypatch.setattr(file_tools, "_profile_scope_mod", lambda: SimpleNamespace(is_scoped=lambda p: False, resolve_profile=lambda: "alice"))
    result = SimpleNamespace(matches=[SimpleNamespace(path=private, content="secret"), SimpleNamespace(path=allowed, content="public")],
                             files=[private, allowed], counts={private: 1, allowed: 2})
    with governance_context(context()):
        assert file_tools._filter_profile_scope_search_results(result) == 3
    assert [match.content for match in result.matches] == ["public"]
    assert result.files == [allowed]
    assert result.counts == {allowed: 2}


def test_final_dispatch_executes_only_after_automatic_acceptance(fixture, monkeypatch):
    import model_tools
    _, _, context, _ = fixture
    calls = model(monkeypatch)
    dispatched = []
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda name, args, **kw: dispatched.append((name, deepcopy(args))) or '{"ok":true}')
    with governance_context(context()):
        result = model_tools.handle_function_call("web_search", {"query": "public"}, session_id="alice-session",
                                                  skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True)
    assert json.loads(result).get("ok") is True
    assert len(dispatched) == len(calls) == 1
    model(monkeypatch, "deny")
    with governance_context(context()):
        result = model_tools.handle_function_call("web_search", {"query": "private"}, session_id="alice-session",
                                                  skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True)
    assert "error" in json.loads(result)
    assert len(dispatched) == 1
