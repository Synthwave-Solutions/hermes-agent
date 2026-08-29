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
    usage_caps=None,
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
        usage_caps=dict(usage_caps or {}),
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

        assert result["error"].startswith("Tool denied by dashboard governance: file_write_root_not_allowed")
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

        assert result["error"].startswith("Tool denied by dashboard governance: cli_command_not_allowed")
        assert result["governance"]["tool"] == "terminal"

    def test_enforce_mode_blocks_shell_operator_even_for_allowed_command(self, monkeypatch):
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
            result = json.loads(model_tools.handle_function_call("terminal", {"command": "git status && rm -rf /tmp/nope"}))

        assert result["error"].startswith("Tool denied by dashboard governance: cli_command_not_allowed")
        assert result["governance"]["tool"] == "terminal"

    def test_enforce_mode_checks_arguments_after_request_middleware_rewrite(self, monkeypatch):
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
        monkeypatch.setattr(
            "model_tools.registry.dispatch",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run")),
        )
        monkeypatch.setattr(
            "hermes_cli.middleware.apply_tool_request_middleware",
            lambda tool_name, args, **context: SimpleNamespace(
                payload={"command": "git status && rm -rf /tmp/nope"},
                original_payload=args,
                changed=True,
                trace=[{"source": "test-middleware", "reason": "rewrite"}],
            ),
        )

        with governance_context(ctx):
            result = json.loads(model_tools.handle_function_call("terminal", {"command": "git status"}))

        assert result["error"].startswith("Tool denied by dashboard governance: cli_command_not_allowed")
        assert result["governance"]["tool"] == "terminal"

    def test_enforce_mode_blocks_tool_call_after_daily_cap(self, monkeypatch, tmp_path):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(tools=["web_search"], usage_caps={"daily_tool_calls": 1}),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "web")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="web", schema={}))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: json.dumps({"ok": True}))
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: False)

        with governance_context(ctx):
            first = json.loads(model_tools.handle_function_call("web_search", {"query": "one"}))
            second = json.loads(model_tools.handle_function_call("web_search", {"query": "two"}))

        assert first == {"ok": True}
        assert second["error"] == "Tool denied by dashboard governance: daily_tool_calls_exceeded"
        assert second["governance"]["tool"] == "web_search"

    def test_usage_cap_slot_reserved_before_dispatch(self, monkeypatch, tmp_path):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(tools=["web_search"], usage_caps={"daily_tool_calls": 1}),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "web")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="web", schema={}))
        seen = []
        monkeypatch.setattr("model_tools.registry.dispatch", lambda name, args, **kwargs: seen.append((name, args)) or json.dumps({"ok": True}))
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: False)

        with governance_context(ctx):
            first = json.loads(model_tools.handle_function_call("web_search", {"query": "first"}))
            second = json.loads(model_tools.handle_function_call("web_search", {"query": "second"}))

        assert first == {"ok": True}
        assert second["error"] == "Tool denied by dashboard governance: daily_tool_calls_exceeded"
        assert seen == [("web_search", {"query": "first"})]

    def test_enforce_mode_blocks_file_write_after_daily_cap(self, monkeypatch, tmp_path):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(
                tools=["write_file"],
                file_write_roots=[str(allowed_root)],
                usage_caps={"daily_file_writes": 1},
            ),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "file")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="file", schema={}))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: json.dumps({"ok": True}))
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: False)

        with governance_context(ctx):
            first = json.loads(model_tools.handle_function_call("write_file", {"path": str(allowed_root / "one.txt"), "content": "x"}))
            second = json.loads(model_tools.handle_function_call("write_file", {"path": str(allowed_root / "two.txt"), "content": "x"}))

        assert first == {"ok": True}
        assert second["error"] == "Tool denied by dashboard governance: daily_file_writes_exceeded"
        assert second["governance"]["tool"] == "write_file"

    def test_enforce_mode_requires_mcp_server_and_tool_allowlist(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(toolsets=["mcp-github"], mcp_servers=["github"], mcp_tools={"github": ["list_issues"]}),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "mcp-github")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="mcp-github", schema={}))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: json.dumps({"ok": True}))
        monkeypatch.setattr("hermes_cli.plugins.has_hook", lambda name: False)

        with governance_context(ctx):
            allowed = json.loads(model_tools.handle_function_call("mcp_github_list_issues", {}))
            denied = json.loads(model_tools.handle_function_call("mcp_github_delete_repo", {}))

        assert allowed == {"ok": True}
        assert denied["error"] == "Tool denied by dashboard governance: tool_not_allowed"

    def test_mcp_toolset_grant_alone_does_not_bypass_mcp_policy(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=_access(toolsets=["mcp-github"]),
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "mcp-github")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="mcp-github", schema={}))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run")))

        with governance_context(ctx):
            result = json.loads(model_tools.handle_function_call("mcp_github_list_issues", {}))

        assert result["error"] == "Tool denied by dashboard governance: tool_not_allowed"

    def test_skill_view_requires_skill_name_grant(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        grants = GrantSet(tools=frozenset({"skill_view"}), skills_view=frozenset({"allowed-skill"}))
        access = EffectiveAccess(subject=GovernanceSubject(email="operator@example.test"), mode="enforce", grants=grants)
        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=access,
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "skills")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="skills", schema={}))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run")))

        with governance_context(ctx):
            result = json.loads(model_tools.handle_function_call("skill_view", {"name": "blocked-skill"}))

        assert result["error"].startswith("Tool denied by dashboard governance: skill_not_allowed")

    def test_skill_manage_requires_manage_grant(self, monkeypatch):
        from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context

        grants = GrantSet(tools=frozenset({"skill_manage"}), skills_manage=frozenset({"allowed-skill"}))
        access = EffectiveAccess(subject=GovernanceSubject(email="operator@example.test"), mode="enforce", grants=grants)
        ctx = DashboardGovernanceContext(
            subject=GovernanceSubject(email="operator@example.test"),
            access=access,
            active_profile="default",
            session_id="session-1",
            request_id="request-1",
        )
        monkeypatch.setattr(model_tools.registry, "get_toolset_for_tool", lambda name: "skills")
        monkeypatch.setattr(model_tools.registry, "get_entry", lambda name: SimpleNamespace(toolset="skills", schema={}))
        monkeypatch.setattr("model_tools.registry.dispatch", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dispatch should not run")))

        with governance_context(ctx):
            result = json.loads(model_tools.handle_function_call("skill_manage", {"name": "blocked-skill", "action": "patch"}))

        assert result["error"].startswith("Tool denied by dashboard governance: skill_manage_not_allowed")


class TestHeredocBodiesAreData:
    """A heredoc body is stdin for one command, not a list of commands.

    Reported by Hrishikesh Oemraw on 28 Aug 2026: every request came back as a
    governance denial. His agent ran an ordinary python heredoc whose body held
    `d=json.load(r); print('URL',u)`; the gate split on that semicolon and
    refused the fragment as cli_command_not_allowed (print(URL,u)). The same
    shape blocked every governed user, since agents write python this way.
    """

    def _decide(self, command: str):
        from hermes_cli.dashboard_governance.tool_policy import _check_cli_command

        return _check_cli_command(command, _access(cli_commands=("python3", "cat", "grep", "git")).grants)

    def test_the_exact_command_that_was_refused_is_allowed(self):
        command = (
            "python3 - <<'PY'\n"
            "import json,urllib.request\n"
            "for u in ['a','b']:\n"
            " with urllib.request.urlopen(u) as r:\n"
            "  d=json.load(r); print('URL',u)\n"
            "PY"
        )
        assert self._decide(command).allowed is True

    @pytest.mark.parametrize("body", ["x = a | b", "a and b; c", "d && e", "s = 'x' # ;|&"])
    def test_shell_operators_inside_a_body_are_text(self, body):
        assert self._decide(f"python3 - <<'PY'\n{body}\nPY").allowed is True

    def test_a_bare_delimiter_still_gates_what_the_shell_would_run(self):
        """<<PY expands, so a substitution in the body is a real command."""
        decision = self._decide("cat <<PY\n$(rm -rf /x)\nPY")
        assert decision.allowed is False
        assert decision.detail == "rm"

    def test_a_quoted_delimiter_makes_the_same_text_literal(self):
        assert self._decide("python3 - <<'PY'\ns = '$(rm -rf /x)'\nPY").allowed is True

    def test_a_command_word_before_the_heredoc_is_still_checked(self):
        assert self._decide("nmap - <<'PY'\nprint(1)\nPY").allowed is False

    def test_a_herestring_is_not_a_heredoc(self):
        assert self._decide("grep foo <<< 'bar'").allowed is True

    def test_backticks_and_process_substitution_stay_refused(self):
        assert self._decide("echo `rm -rf /x`").allowed is False
        assert self._decide("cat <(rm -rf /x)").allowed is False

    def test_an_ordinary_compound_command_is_unchanged(self):
        assert self._decide("git status && rm -rf /x").allowed is False
        assert self._decide("cat a.txt | grep b").allowed is True


class TestDwdIdentityBinding:
    """Hard requirement 29-08-2026: a governed non-admin may only drive their
    OWN Google Workspace account. Every gchat/gmail/gws-hermes/gdrive-dwd call
    must carry `--as <own email>`; without it the CLI would run on the owner's
    token (as Michael), with another address it would impersonate someone."""

    ME = "stephen@synthwave.solutions"

    def _access(self, *, roles=("tech_lead",), sources=(), email=ME, cli=("gchat", "gmail", "gws-hermes", "git", "echo", "head")):
        return EffectiveAccess(
            subject=GovernanceSubject(email=email),
            mode="enforce",
            roles=frozenset(roles),
            grants=GrantSet(cli_commands=frozenset(cli)),
            grant_sources=tuple(sources),
        )

    def _decide(self, access, command):
        from hermes_cli.dashboard_governance.tool_policy import decide_tool_argument_access
        return decide_tool_argument_access(access, "terminal", {"command": command})

    def test_send_without_as_is_refused(self):
        d = self._decide(self._access(), "gchat send --space spaces/x --text hi")
        assert not d.allowed and d.reason == "dwd_identity_required" and d.detail == "gchat"

    def test_read_without_as_is_refused_too(self):
        d = self._decide(self._access(), "gchat messages --space spaces/x --limit 5")
        assert not d.allowed and d.reason == "dwd_identity_required"

    def test_as_self_is_allowed(self):
        assert self._decide(self._access(), f"gchat --as {self.ME} send --space spaces/x --text hi").allowed
        assert self._decide(self._access(), f"gmail --as={self.ME} list --limit 3").allowed
        assert self._decide(self._access(), f"gws-hermes --as {self.ME} gmail messages list").allowed

    def test_as_self_is_case_insensitive(self):
        assert self._decide(self._access(), "gchat --as Stephen@Synthwave.Solutions spaces").allowed

    def test_as_someone_else_is_refused(self):
        d = self._decide(self._access(), "gchat --as michael@synthwave.solutions send --space spaces/x --text hi")
        assert not d.allowed and d.reason == "dwd_identity_mismatch"
        assert d.detail == "gchat --as michael@synthwave.solutions"

    def test_dangling_as_is_refused(self):
        assert not self._decide(self._access(), "gmail --as").allowed

    def test_env_prefix_cannot_substitute_for_as(self):
        # gchat honours GCHAT_WRITE_AS only when --as is absent; the gate still
        # demands an explicit --as self, so the env prefix buys nothing.
        d = self._decide(self._access(), "GCHAT_WRITE_AS=michael@synthwave.solutions gchat send --space spaces/x --text hi")
        assert not d.allowed and d.reason == "dwd_identity_required"
        assert self._decide(self._access(), f"GCHAT_WRITE_AS=x gchat --as {self.ME} send --space spaces/x --text hi").allowed

    def test_every_segment_is_checked(self):
        assert self._decide(self._access(), f"git status && gchat --as {self.ME} spaces").allowed
        d = self._decide(self._access(), f"gchat --as {self.ME} spaces; gmail list")
        assert not d.allowed and d.detail == "gmail"

    def test_command_substitution_and_heredoc_are_checked(self):
        assert not self._decide(self._access(), "echo $(gchat spaces)").allowed
        assert not self._decide(self._access(), "cat <<EOF\n$(gmail list)\nEOF").allowed
        assert self._decide(self._access(), f"echo $(gchat --as {self.ME} spaces)").allowed

    def test_full_path_invocation_is_checked(self):
        d = self._decide(self._access(cli=("*",)), "/home/synthwavehq/.local/bin/gchat send --space spaces/x --text hi")
        assert not d.allowed and d.reason == "dwd_identity_required"

    def test_wildcard_cli_grant_does_not_lift_the_binding(self):
        d = self._decide(self._access(cli=("*",)), "gmail send --to a@b.nl --subject s --text t")
        assert not d.allowed and d.reason == "dwd_identity_required"

    def test_unknown_identity_fails_closed(self):
        d = self._decide(self._access(email=""), "gchat --as stephen@synthwave.solutions spaces")
        assert not d.allowed and d.reason == "dwd_identity_mismatch"

    def test_bootstrap_admin_is_unrestricted(self):
        a = self._access(email="michael@synthwave.solutions", roles=(), sources=("bootstrap_admin",), cli=("*",))
        assert self._decide(a, "gchat send --space spaces/x --text hi").allowed
        assert self._decide(a, "gmail --as info@synthwave.solutions list").allowed

    def test_owner_and_admin_roles_are_unrestricted(self):
        for role in ("owner", "admin"):
            a = self._access(email="yaser@synthwave.solutions", roles=(role,), cli=("*",))
            assert self._decide(a, "gchat --as odis@synthwave.solutions spaces").allowed

    def test_non_dwd_commands_are_untouched(self):
        assert self._decide(self._access(), "git log --oneline -3 | head").allowed
        assert self._decide(self._access(), "echo --as nobody@x.nl").allowed

    def test_governance_off_is_untouched(self):
        a = EffectiveAccess(subject=GovernanceSubject(email=self.ME), mode="report_only", grants=GrantSet())
        assert self._decide(a, "gchat send --space spaces/x --text hi").allowed

    def test_model_tools_denial_gives_the_fix_and_files_nothing(self):
        payload = model_tools._governance_denial_payload(None, "terminal", "dwd_identity_required", "gchat")
        assert payload["access_request"] == "not_applicable"
        assert "--as" in payload["required_behavior"]


class TestSecretsAreOutOfShellReach:
    """29-08-2026: the file tools honoured denied_globs, the terminal did not,
    so a governed user could `cat` the domain-wide-delegation key that
    read_file refused them. Every path in a command line is now checked, and
    the per-person allow_globs exception still opens one named file."""

    KEY = "/home/synthwavehq/.hermes/gmail-dwd-sa.json"

    def _access(self, *, allow=(), cli=("cat", "cp", "grep", "ls", "python3", "git")):
        return EffectiveAccess(
            subject=GovernanceSubject(email="stephen@synthwave.solutions"),
            mode="enforce",
            roles=frozenset({"tech_lead"}),
            grants=GrantSet(
                cli_commands=frozenset(cli),
                file_denied_globs=frozenset({"**/.env", "**/*-sa.json", "**/.hermes/**"}),
                file_allow_globs=frozenset(allow),
            ),
        )

    def _decide(self, access, command):
        from hermes_cli.dashboard_governance.tool_policy import decide_tool_argument_access
        return decide_tool_argument_access(access, "terminal", {"command": command})

    def test_reading_the_delegation_key_is_refused(self):
        d = self._decide(self._access(), f"cat {self.KEY}")
        assert not d.allowed and d.reason == "file_denied_glob" and d.detail == self.KEY

    def test_tilde_path_is_refused(self):
        assert not self._decide(self._access(), "cat ~/.hermes/gmail-dwd-sa.json").allowed

    def test_a_path_quoted_inside_an_interpreter_argument_is_refused(self):
        d = self._decide(self._access(), f"python3 -c \"print(open('{self.KEY}').read())\"")
        assert not d.allowed and d.reason == "file_denied_glob"

    def test_copying_it_out_is_refused(self):
        assert not self._decide(self._access(), f"cp {self.KEY} /tmp/k.json").allowed

    def test_dotenv_files_are_refused(self):
        assert not self._decide(self._access(), "cat /home/synthwavehq/clients/peterson/.env").allowed

    def test_ordinary_client_work_still_runs(self):
        assert self._decide(self._access(), "git -C /home/synthwavehq/clients/adams status").allowed
        assert self._decide(self._access(), "grep -rn TODO /home/synthwavehq/clients/360kas/src").allowed

    def test_a_granted_exception_opens_exactly_that_file(self):
        access = self._access(allow={"/home/synthwavehq/.hermes/skills/x/notify.py"})
        assert self._decide(access, "cat /home/synthwavehq/.hermes/skills/x/notify.py").allowed
        assert not self._decide(access, f"cat {self.KEY}").allowed

    def test_a_caller_without_denied_globs_is_unchanged(self):
        access = EffectiveAccess(
            subject=GovernanceSubject(email="stephen@synthwave.solutions"),
            mode="enforce",
            grants=GrantSet(cli_commands=frozenset({"cat"})),
        )
        assert self._decide(access, f"cat {self.KEY}").allowed


class TestIdentityBindingCannotBeShakenOff:
    """The identity that pins the Google CLIs travels in the child process
    environment, so the gate refuses both the wrapper words that used to hide
    the real command and any attempt to name that variable."""

    ME = "stephen@synthwave.solutions"

    def _access(self, cli=("gchat", "env", "command", "time", "echo")):
        return EffectiveAccess(
            subject=GovernanceSubject(email=self.ME),
            mode="enforce",
            roles=frozenset({"tech_lead"}),
            grants=GrantSet(cli_commands=frozenset(cli)),
        )

    def _decide(self, command, access=None):
        from hermes_cli.dashboard_governance.tool_policy import decide_tool_argument_access
        return decide_tool_argument_access(access or self._access(), "terminal", {"command": command})

    def test_command_wrapper_no_longer_hides_the_call(self):
        d = self._decide("command gchat --as michael@synthwave.solutions spaces")
        assert not d.allowed and d.reason == "dwd_identity_mismatch"

    def test_time_wrapper_no_longer_hides_the_call(self):
        assert not self._decide("time gchat --as michael@synthwave.solutions spaces").allowed

    def test_wrapper_still_passes_an_allowed_call_through(self):
        assert self._decide(f"command gchat --as {self.ME} spaces").allowed

    def test_a_wrapped_command_outside_the_allowlist_is_still_refused(self):
        d = self._decide("command curl https://example.com")
        assert not d.allowed and d.reason == "cli_command_not_allowed"

    def test_clearing_the_identity_variable_is_refused(self):
        d = self._decide("HERMES_DWD_IDENTITY= gchat send --space x --text hi")
        assert not d.allowed and d.reason == "dwd_identity_tamper"

    def test_unsetting_the_identity_variable_is_refused(self):
        assert not self._decide("env -u HERMES_DWD_IDENTITY gchat spaces").allowed

    def test_admins_are_not_bound_and_may_name_the_variable(self):
        admin = EffectiveAccess(
            subject=GovernanceSubject(email="michael@synthwave.solutions"),
            mode="enforce",
            roles=frozenset({"owner", "admin"}),
            grants=GrantSet(cli_commands=frozenset({"echo"})),
            grant_sources=("bootstrap_admin",),
        )
        assert self._decide("echo $HERMES_DWD_IDENTITY", access=admin).allowed
