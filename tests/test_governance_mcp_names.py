"""MCP wire names and local grant names enforce the same policy at dispatch."""
import json
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context
from hermes_cli.dashboard_governance.loader import load_governance_policy
from hermes_cli.dashboard_governance.models import GovernanceSubject
from hermes_cli.dashboard_governance.resolver import resolve_effective_access
from hermes_cli.dashboard_governance.tool_policy import decide_tool_access
from tools.mcp_tool import mcp_prefixed_tool_name


def make_context(tmp_path, server, *, grants=None, denied=None):
    subject = GovernanceSubject(email="reader@example.test")
    policy = {"mode": "enforce", "roles": {"member": {"grants": {
        "permissions": ["chat:use"], "profiles": ["default"],
        "mcp": grants or {"servers": ["*"], "tools": {"*": ["*"]}},
    }}}, "users": {subject.email: {"roles": ["member"], "access_mode": "blacklist",
        "access_level": "elevated", "deny": {"mcp": {"tools": denied or {}}}}}}
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy))
    return DashboardGovernanceContext(subject=subject,
        access=resolve_effective_access(load_governance_policy(path=path), subject),
        active_profile="default", session_id="mcp-test", approval_policy_path=str(path))


@pytest.mark.parametrize("server", ["public", "qa-stdio", "my server.v1"])
@pytest.mark.parametrize("key_style", ["server", "toolset", "wildcard"])
@pytest.mark.parametrize("deny_style", ["local", "canonical"])
def test_native_explicit_tool_deny_wins_at_actual_dispatch(tmp_path, monkeypatch, server, key_style, deny_style):
    import model_tools
    blocked = mcp_prefixed_tool_name(server, "secret")
    allowed = mcp_prefixed_tool_name(server, "public_read")
    key = {"server": server, "toolset": "mcp-" + server, "wildcard": "*"}[key_style]
    ctx = make_context(tmp_path, server, denied={key: ["secret" if deny_style == "local" else blocked]})
    entry = SimpleNamespace(toolset="mcp-" + server, schema={})
    monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: entry)
    monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: entry.toolset)
    calls = []
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda name, args, **kw: calls.append(name) or '{"ok":true}')
    monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: False)
    with governance_context(ctx):
        denied_result = model_tools.handle_function_call(blocked, {},
            skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True)
        allowed_result = model_tools.handle_function_call(allowed, {},
            skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True)
    assert "error" in json.loads(denied_result)
    assert json.loads(allowed_result) == {"ok": True}
    assert calls == [allowed]


@pytest.mark.parametrize("server", ["public", "qa-stdio", "my server.v1"])
@pytest.mark.parametrize("naming", ["canonical", "legacy"])
def test_local_tool_grant_accepts_only_its_registered_server(tmp_path, server, naming):
    ctx = make_context(tmp_path, server, grants={"servers": [server], "tools": {server: ["public_read"]}})
    name = mcp_prefixed_tool_name(server, "public_read") if naming == "canonical" else f"mcp_{server}_public_read"
    registry = SimpleNamespace(get_entry=lambda tool: SimpleNamespace(toolset="mcp-" + server))
    assert decide_tool_access(ctx.access, name, registry).allowed
    assert not decide_tool_access(ctx.access, mcp_prefixed_tool_name(server, "secret"), registry).allowed
    other_registry = SimpleNamespace(get_entry=lambda tool: SimpleNamespace(toolset="mcp-other"))
    assert not decide_tool_access(ctx.access, name, other_registry).allowed
