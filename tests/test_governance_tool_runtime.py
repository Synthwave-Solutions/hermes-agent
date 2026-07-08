from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import model_tools
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet


def _access(
    *,
    mode: str = "enforce",
    tools=(),
    toolsets=(),
    mcp_servers=(),
    mcp_tools=None,
    file_read_roots=(),
    file_write_roots=(),
    file_denied_globs=(),
    cli_commands=(),
    cli_workdir_roots=(),
) -> EffectiveAccess:
    grants = GrantSet(
        tools=frozenset(tools),
        toolsets=frozenset(toolsets),
        mcp_servers=frozenset(mcp_servers),
        mcp_tools={key: frozenset(value) for key, value in (mcp_tools or {}).items()},
        file_read_roots=frozenset(file_read_roots),
        file_write_roots=frozenset(file_write_roots),
        file_denied_globs=frozenset(file_denied_globs),
        cli_commands=frozenset(cli_commands),
        cli_workdir_roots=frozenset(cli_workdir_roots),
    )
    return EffectiveAccess(
        subject=GovernanceSubject(email="operator@example.test"),
        mode=mode,
        grants=grants,
    )


@pytest.fixture(autouse=True)
def _clear_tool_defs_cache():
    model_tools._clear_tool_defs_cache()
    yield
    model_tools._clear_tool_defs_cache()


def _tool(name: str):
    return {"type": "function", "function": {"name": name, "description": f"{name} tool"}}


class TestDashboardGovernanceContext:
    def test_contextvar_binding_restores_previous_context(self):
        from hermes_cli.dashboard_governance.context import (
            DashboardGovernanceContext,
            current_governance_context,
            governance_context,
        )

        first = DashboardGovernanceContext(
            subject=GovernanceSubject(email="first@example.test"),
            access=_access(tools=["read_file"]),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        second = DashboardGovernanceContext(
            subject=GovernanceSubject(email="second@example.test"),
            access=_access(tools=["terminal"]),
            active_profile="ops",
            session_id="session-2",
            request_id="request-2",
        )

        assert current_governance_context() is None
        with governance_context(first):
            assert current_governance_context() is first
            with governance_context(second):
                assert current_governance_context() is second
            assert current_governance_context() is first
        assert current_governance_context() is None

    def test_env_payload_rehydrates_context_for_subprocesses(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import (
            DashboardGovernanceContext,
            GOVERNANCE_CONTEXT_ENV,
            current_governance_context,
            serialize_context_for_env,
        )

        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(tools=["read_file"], toolsets=["web"]),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setenv(GOVERNANCE_CONTEXT_ENV, serialize_context_for_env(ctx))

        restored = current_governance_context()
        assert restored is not None
        assert restored.access.mode == "enforce"
        assert restored.access.grants.tools == frozenset({"read_file"})
        assert restored.access.grants.toolsets == frozenset({"web"})
        assert restored.subject.email == "operator@example.test"


class TestGovernanceToolSchemaFiltering:
    def test_enforce_mode_filters_model_tool_schemas_to_allowed_tools(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        monkeypatch.setattr(
            model_tools.registry,
            "get_definitions",
            lambda names, quiet=True: [_tool(name) for name in sorted(names)],
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "file" if name == "read_file" else "terminal")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset=model_tools.registry.get_toolset_for_tool(name)))
        monkeypatch.setattr("model_tools.resolve_toolset", lambda name: {"read_file", "terminal"} if name == "mixed" else set())
        monkeypatch.setattr("model_tools.validate_toolset", lambda name: name == "mixed")

        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(tools=["read_file"]),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        with governance_context(ctx):
            defs = model_tools.get_tool_definitions(enabled_toolsets=["mixed"], quiet_mode=True)

        names = {tool["function"]["name"] for tool in defs}
        assert names == {"read_file"}

    def test_enforce_mode_allows_tools_by_allowed_toolset(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        monkeypatch.setattr(
            model_tools.registry,
            "get_definitions",
            lambda names, quiet=True: [_tool(name) for name in sorted(names)],
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "terminal")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="terminal", schema={}))
        monkeypatch.setattr("model_tools.resolve_toolset", lambda name: {"terminal"} if name == "terminal" else set())
        monkeypatch.setattr("model_tools.validate_toolset", lambda name: name == "terminal")

        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(toolsets=["terminal"]),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        with governance_context(ctx):
            defs = model_tools.get_tool_definitions(enabled_toolsets=["terminal"], quiet_mode=True)

        assert [tool["function"]["name"] for tool in defs] == ["terminal"]

    def test_report_only_mode_does_not_filter_schemas(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        monkeypatch.setattr(
            model_tools.registry,
            "get_definitions",
            lambda names, quiet=True: [_tool(name) for name in sorted(names)],
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "file")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="file", schema={}))
        monkeypatch.setattr("model_tools.resolve_toolset", lambda name: {"read_file", "write_file"} if name == "file" else set())
        monkeypatch.setattr("model_tools.validate_toolset", lambda name: name == "file")

        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(mode="report_only"),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        with governance_context(ctx):
            defs = model_tools.get_tool_definitions(enabled_toolsets=["file"], quiet_mode=True)

        names = {tool["function"]["name"] for tool in defs}
        assert names == {"read_file", "write_file"}


class TestGovernanceToolDispatch:
    def test_enforce_mode_blocks_disallowed_tool_before_hooks_and_dispatch(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(tools=["read_file"]),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "terminal")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="terminal", schema={}))
        monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("hook should not fire")))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run")))

        with governance_context(ctx):
            result = json.loads(model_tools.handle_function_call("terminal", {"command": "pwd"}))

        assert result["error"] == "Tool denied by dashboard governance: tool_not_allowed"
        assert result["governance"]["mode"] == "enforce"
        assert result["governance"]["tool"] == "terminal"

    def test_enforce_mode_allows_explicitly_granted_tool(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(tools=["web_search"]),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "web")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="web", schema={}))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: json.dumps({"ok": True}))
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: False)

        with governance_context(ctx):
            result = json.loads(model_tools.handle_function_call("web_search", {"query": "test"}))

        assert result == {"ok": True}

    def test_enforce_mode_blocks_write_file_outside_write_root(self, monkeypatch, tmp_path):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        outside = tmp_path / "outside.txt"
        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(tools=["write_file"], file_write_roots=[str(allowed_root)]),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "file")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="file", schema={}))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run")))

        with governance_context(ctx):
            result = json.loads(model_tools.handle_function_call("write_file", {"path": str(outside), "content": "x"}))

        assert result["error"] == "Tool denied by dashboard governance: file_write_root_not_allowed"
        assert result["governance"]["tool"] == "write_file"

    def test_enforce_mode_blocks_terminal_command_not_in_allowlist(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(tools=["terminal"], cli_commands=["git"]),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "terminal")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="terminal", schema={}))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run")))

        with governance_context(ctx):
            result = json.loads(model_tools.handle_function_call("terminal", {"command": "rm -rf /tmp/nope"}))

        assert result["error"] == "Tool denied by dashboard governance: cli_command_not_allowed"
        assert result["governance"]["tool"] == "terminal"
