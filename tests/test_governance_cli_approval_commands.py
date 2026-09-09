"""CLI selectors must park real dispatch without granting command authority."""
from copy import deepcopy
from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.dashboard_governance import action_approval as review
from hermes_cli.dashboard_governance.context import (
    DashboardGovernanceContext, governance_context, serialize_context_for_env,
)
from hermes_cli.dashboard_governance.loader import load_governance_policy
from hermes_cli.dashboard_governance.models import GovernanceSubject, EffectiveAccess, GrantSet
from hermes_cli.dashboard_governance.resolver import resolve_effective_access
from hermes_cli.dashboard_governance.tool_policy import cli_command_requires_manual_approval, decide_tool_argument_access


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import model_tools
    from tools import approval
    subject = GovernanceSubject(email="cli@example.test")
    role = {"permissions": ["chat:use", "terminal:use"], "profiles": ["default"],
            "tools": {"builtins": ["terminal"]},
            "cli": {"commands": ["*"], "workdir_roots": [str(tmp_path)]}}
    user = {"roles": ["member"], "access_level": "elevated", "access_mode": "blacklist",
            "grants": {"cli": {"approval_commands": ["touch"]}}}
    policy = {"mode": "enforce", "roles": {"member": {"grants": role}}, "users": {subject.email: user}}
    path = tmp_path / "policy.yaml"
    def write():
        path.write_text(yaml.safe_dump(policy))
    write()
    def context():
        return DashboardGovernanceContext(subject=subject,
            access=resolve_effective_access(load_governance_policy(path=path), subject),
            active_profile="default", session_id="cli-test", approval_policy_path=str(path))
    effect = tmp_path / "effect.txt"
    dispatched, audits = [], []
    entry = SimpleNamespace(toolset="terminal", schema={})
    monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: entry)
    monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: entry.toolset)
    def dispatch(name, args, **kwargs):
        assert name == "terminal"
        dispatched.append(args["command"])
        completed = subprocess.run(args["command"], shell=True, cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=5)
        return json.dumps({"exit_code": completed.returncode, "output": completed.stdout})
    monkeypatch.setattr(model_tools.registry, "dispatch", dispatch)
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: False)
    monkeypatch.setattr(review, "_audit", lambda *args: audits.append(args))
    def run(command="/usr/bin/touch effect.txt", ctx=None):
        with governance_context(ctx or context()):
            return json.loads(model_tools.handle_function_call("terminal", {"command": command, "workdir": str(tmp_path)},
                session_id="cli-test", skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True))
    token = approval.set_current_session_key("cli-test")
    yield SimpleNamespace(user=user, role=role, write=write, context=context, run=run,
                          effect=effect, dispatched=dispatched, audits=audits)
    approval.unregister_gateway_notify("cli-test")
    approval.reset_current_session_key(token)


def respond_manually(runtime, *, choice="once", before=None):
    from tools import approval
    shown = []
    def notify(data):
        assert not runtime.effect.exists()
        assert not runtime.dispatched
        assert data["allow_session"] is False
        assert data["allow_permanent"] is False
        shown.append(data)
        if before:
            before()
        approval.resolve_gateway_approval("cli-test", choice, request_id=data["request_id"])
    approval.register_gateway_notify("cli-test", notify)
    return shown


@pytest.mark.parametrize("mode", ["legacy", "blacklist", "whitelist"])
@pytest.mark.parametrize("general_approval", [None, "automatic"])
def test_matching_selector_requires_human_before_real_file_effect(runtime, monkeypatch, mode, general_approval):
    if mode == "legacy":
        runtime.user.pop("access_mode")
        runtime.user.pop("access_level")
    elif mode == "whitelist":
        runtime.user["access_mode"] = mode
        runtime.user["grants"] = deepcopy(runtime.role)
        runtime.user["grants"]["cli"]["approval_commands"] = ["touch"]
    if general_approval:
        runtime.user["approval"] = {"mode": general_approval, "prompt": "Allow terminal work."}
    runtime.write()
    monkeypatch.setattr(review, "_ask_model", lambda *args: pytest.fail("a required command cannot be AI approved"))
    shown = respond_manually(runtime)
    assert runtime.run()["exit_code"] == 0
    assert runtime.effect.read_bytes() == b""
    assert len(shown) == len(runtime.dispatched) == 1
    assert runtime.audits[-1][-3:-1] == ("manual", "approve")


def test_manual_deny_prevents_real_effect_and_consumes_only_one_request(runtime, monkeypatch):
    from tools import approval
    monkeypatch.setattr(review, "_ask_model", lambda *args: pytest.fail("must remain manual"))
    shown = respond_manually(runtime, choice="deny")
    assert "error" in runtime.run()
    assert not runtime.effect.exists()
    assert not runtime.dispatched
    assert len(shown) == 1
    assert not approval.list_gateway_approvals("cli-test")


def test_comment_cannot_hide_next_command_from_manual_review(runtime):
    shown = respond_manually(runtime)
    assert runtime.run("printf ok # comment\ntouch effect.txt")["exit_code"] == 0
    assert len(shown) == 1 and runtime.effect.exists()


def test_commented_heredoc_opener_cannot_hide_real_executable(runtime):
    shown = respond_manually(runtime)
    assert runtime.run("printf ok # <<EOF\ntouch effect.txt\n# EOF")["exit_code"] == 0
    assert len(shown) == 1 and runtime.effect.exists()


def test_quoted_heredoc_example_cannot_hide_real_executable(runtime):
    shown = respond_manually(runtime)
    assert runtime.run('printf "<<EOF "\ntouch effect.txt\n# EOF')["exit_code"] == 0
    assert len(shown) == 1 and runtime.effect.exists()


@pytest.mark.parametrize("command", [
    "if touch effect.txt; then printf ok; fi",
    "! touch effect.txt",
    "while touch effect.txt; do break; done",
    "(touch effect.txt)",
])
def test_compound_command_with_review_rule_parks_before_real_effect(runtime, monkeypatch, command):
    runtime.user["approval"] = {"mode": "automatic", "prompt": "Allow terminal work."}
    runtime.write()
    monkeypatch.setattr(review, "_ask_model", lambda *args: pytest.fail("unsupported compound cannot be AI approved"))
    shown = respond_manually(runtime)
    assert "error" not in runtime.run(command)
    assert len(shown) == 1 and runtime.effect.exists()


@pytest.mark.parametrize("constraint", ["deny", "finite_allow"])
@pytest.mark.parametrize("command", [
    "if touch effect.txt; then printf ok; fi",
    "! touch effect.txt",
    "while touch effect.txt; do break; done",
    "f() { touch effect.txt; }; f",
    "(touch effect.txt)",
])
def test_compound_command_cannot_override_hard_command_constraints(runtime, constraint, command):
    if constraint == "deny":
        runtime.user["deny"] = {"cli": {"commands": ["touch"]}}
    else:
        runtime.role["cli"]["commands"] = ["touch", "printf"]
    runtime.write()
    shown = respond_manually(runtime)
    assert "cli_compound_command_not_allowed" in runtime.run(command)["error"]
    assert not shown and not runtime.dispatched and not runtime.effect.exists()


def test_deny_section_cannot_turn_off_required_review(runtime):
    runtime.user["deny"] = {"cli": {"approval_commands": ["touch", "*"]}}
    runtime.write()
    shown = respond_manually(runtime)
    assert runtime.run()["exit_code"] == 0
    assert len(shown) == 1


def test_approval_selector_never_grants_missing_command_permission(runtime):
    runtime.role["cli"]["commands"] = ["printf"]
    runtime.write()
    shown = respond_manually(runtime)
    assert "cli_command_not_allowed" in runtime.run()["error"]
    assert not shown and not runtime.dispatched and not runtime.effect.exists()


def test_required_review_without_authoritative_policy_source_fails_closed(runtime):
    shown = respond_manually(runtime)
    assert "governance_review_unavailable" in runtime.run(ctx=replace(runtime.context(), approval_policy_path=""))["error"]
    assert not shown and not runtime.dispatched and not runtime.effect.exists()


@pytest.mark.parametrize("general_approval", [None, "automatic", "manual"])
def test_nonmatching_command_preserves_existing_approval_behavior(runtime, monkeypatch, general_approval):
    from tools import approval
    if general_approval:
        runtime.user["approval"] = {"mode": general_approval, "prompt": "Allow public output."}
    runtime.write()
    model_calls = []
    monkeypatch.setattr(review, "_ask_model", lambda *args: model_calls.append(args) or {
        "decision": "approve", "reason": "Public output allowed.", "confidence": 1})
    if general_approval == "manual":
        shown = respond_manually(runtime)
    else:
        shown = []
        monkeypatch.setattr(approval, "request_governance_action_approval", lambda *args: pytest.fail("unmatched rule cannot require manual review"))
    assert runtime.run("printf public")["output"] == "public"
    assert len(model_calls) == (1 if general_approval == "automatic" else 0)
    assert len(shown) == (1 if general_approval == "manual" else 0)


@pytest.mark.parametrize("when", ["before", "during"])
def test_command_deny_cannot_be_overridden_by_manual_once(runtime, monkeypatch, when):
    monkeypatch.setattr(review, "_ask_model", lambda *args: pytest.fail("deny cannot reach AI"))
    def revoke():
        runtime.user["deny"] = {"cli": {"commands": ["touch"]}}
        runtime.write()
    if when == "before":
        revoke()
        shown = respond_manually(runtime)
    else:
        shown = respond_manually(runtime, before=revoke)
    result = runtime.run()
    assert "cli_command_denied" in result["error"]
    assert not runtime.dispatched and not runtime.effect.exists()
    assert len(shown) == (1 if when == "during" else 0)


def test_continuation_uses_new_current_command_review_rule(runtime, monkeypatch):
    runtime.user["grants"]["cli"]["approval_commands"] = []
    runtime.write()
    original = runtime.context()
    resumed = replace(original, continuation_contexts=(serialize_context_for_env(original),))
    runtime.user["grants"]["cli"]["approval_commands"] = ["touch"]
    runtime.write()
    monkeypatch.setattr(review, "_ask_model", lambda *args: pytest.fail("fresh mandatory manual rule required"))
    shown = respond_manually(runtime)
    assert runtime.run(ctx=resumed)["exit_code"] == 0
    assert len(shown) == 1 and runtime.effect.exists()


def test_continuation_preserves_hard_deny_even_when_current_policy_removes_it(runtime, monkeypatch):
    runtime.user["deny"] = {"cli": {"commands": ["touch"]}}
    runtime.write()
    original = runtime.context()
    runtime.user.pop("deny")
    runtime.write()
    resumed = replace(runtime.context(), continuation_contexts=(serialize_context_for_env(original),))
    monkeypatch.setattr(review, "_ask_model", lambda *args: pytest.fail("retained deny cannot reach AI"))
    shown = respond_manually(runtime)
    assert "error" in runtime.run(ctx=resumed)
    assert not shown and not runtime.dispatched and not runtime.effect.exists()


def test_continuation_uses_current_review_mode_without_replaying_historical_manual_rule(runtime, monkeypatch):
    original = runtime.context()
    runtime.user["grants"]["cli"]["approval_commands"] = []
    runtime.user["approval"] = {"mode": "automatic", "prompt": "Allow terminal work."}
    runtime.write()
    resumed = replace(runtime.context(), continuation_contexts=(serialize_context_for_env(original),))
    model_calls = []
    monkeypatch.setattr(review, "_ask_model", lambda *args: model_calls.append(args) or {
        "decision": "approve", "reason": "Terminal work allowed.", "confidence": 1})
    from tools import approval
    monkeypatch.setattr(approval, "request_governance_action_approval", lambda *args: pytest.fail("only current review rules apply"))
    assert runtime.run(ctx=resumed)["exit_code"] == 0
    assert runtime.effect.exists() and len(model_calls) == 1


@pytest.mark.parametrize("command,required", [
    ("touch file", True), ("/usr/bin/touch file", True), ("printf touch", False),
    ("printf ok; touch file", True), ("printf ok\ntouch file", True),
    ("printf ok # comment\ntouch file", True),
    ("printf ok # <<EOF\ntouch file\nEOF", True),
    ('printf "<<EOF "\ntouch file\nEOF', True),
    ('printf "example\n<<EOF "\ntouch file\nEOF', True),
    ("printf '# touch file'", False), ("printf \"# touch file\"", False),
    ("printf x#word\ntouch file", True), ("printf \\#literal\ntouch file", True),
    ("printf ok # touch file", False),
    ("printf '%s' $(touch file)", True), ("printf '%s' $(printf '%s' $(touch file))", True),
    ("env X=ok command touch file", True),
    ("cat <<EOF\n$(touch file)\nEOF", True), ("cat <<'EOF'\ntouch file\nEOF", False),
    ("cat <<EOF\n# $(touch file)\nEOF", True),
    ("cat <<'EOF'\n# $(touch file)\nEOF", False),
    ("printf 'unterminated", True),
])
def test_review_selectors_share_permission_parser(command, required):
    access = EffectiveAccess(subject=GovernanceSubject(email="test@example.test"), mode="enforce",
                             grants=GrantSet(cli_approval_commands=frozenset({"touch"})))
    assert cli_command_requires_manual_approval(access, command) is required


@pytest.mark.parametrize("command", ["printf ok\ntouch file", "printf ok # comment\ntouch file", "printf ok # <<EOF\ntouch file\nEOF"])
def test_newline_cannot_hide_denied_second_command(command):
    access = EffectiveAccess(subject=GovernanceSubject(email="test@example.test"), mode="enforce",
                             grants=GrantSet(cli_commands=frozenset({"*"}), cli_denied_commands=frozenset({"touch"})))
    assert not decide_tool_argument_access(access, "terminal", {"command": command}).allowed
