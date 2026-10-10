"""Built-in file-tool hard deny for the SynthPulse secret mount (plan 4.3, E3).

Pair tokens, the memory hub actor key and other installation secrets are
mounted under /run/synthpulse/secrets. The agent's file tools (read_file,
search_files, write_file, patch) never reach below that root: for every
caller, administrators included, in every governance mode and without a
governance context at all. The deny is built in, not a policy `deny.files`
entry, so it leaves host_execution_restricted and therefore terminal access
exactly as they were.
"""
from __future__ import annotations

import json
import os

import pytest

import model_tools
from hermes_cli.dashboard_governance import tool_policy
from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context
from hermes_cli.dashboard_governance.loader import parse_governance_policy
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from hermes_cli.dashboard_governance.resolver import resolve_effective_access
from hermes_cli.dashboard_governance.tool_policy import (
    decide_tool_argument_access,
    tool_allowed_for_context,
    tool_arguments_allowed_for_context,
)

SECRET = "/run/synthpulse/secrets/x"
REASON = "secret_path_hard_deny"


class _Registry:
    def get_entry(self, name):
        return None

    def get_toolset_for_tool(self, name):
        return {"terminal": "terminal", "read_file": "file", "write_file": "file",
                "patch": "file", "search_files": "file"}.get(name, "")


def _ctx(access: EffectiveAccess, **extra) -> DashboardGovernanceContext:
    return DashboardGovernanceContext(subject=access.subject, access=access, **extra)


def _blacklist_access(mode: str = "enforce") -> EffectiveAccess:
    """An elevated blacklist account: every file root and terminal by default."""
    raw = {"mode": mode, "roles": {"operator": {"grants": {"permissions": ["chat:use"]}}}, "users": {
        "power.user@acme.example": {"access_level": "elevated", "access_mode": "blacklist", "roles": ["operator"]}}}
    return resolve_effective_access(parse_governance_policy(raw), GovernanceSubject(email="power.user@acme.example"))


def _bootstrap_access() -> EffectiveAccess:
    raw = {"mode": "enforce", "bootstrap_admins": ["ops@synthwave.example"]}
    return resolve_effective_access(parse_governance_policy(raw), GovernanceSubject(email="ops@synthwave.example"))


def _all_accesses():
    return {
        "blacklist_enforce": _blacklist_access(),
        "blacklist_report_only": _blacklist_access("report_only"),
        "bootstrap_admin": _bootstrap_access(),
        "wide_whitelist": EffectiveAccess(
            subject=GovernanceSubject(email="wide@acme.example"), mode="enforce",
            grants=GrantSet(tools=frozenset({"*"}), file_read_roots=frozenset({"/"}),
                            file_write_roots=frozenset({"/"}), file_allow_globs=frozenset({"*"}))),
    }


FILE_CALLS = (
    ("read_file", {"path": SECRET}),
    ("read_file", {"path": "/run/synthpulse/secrets"}),
    ("read_file", {"path": "/run/synthpulse/../synthpulse/secrets/x"}),
    ("read_file", {"path": "/run/./synthpulse/secrets//a2a/pair.token"}),
    ("read_file", {"path": "/run/synthpulse/secrets/"}),
    ("search_files", {"path": "/run/synthpulse/secrets", "pattern": "a2a1"}),
    ("write_file", {"path": SECRET, "content": "x"}),
    ("patch", {"path": SECRET, "old_string": "a", "new_string": "b"}),
    ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: /run/synthpulse/secrets/x\n@@\n-a\n+b\n*** End Patch"}),
    ("patch", {"mode": "patch", "patch": "*** Begin Patch\n***Add File: /run/synthpulse/secrets/new\n+x\n*** End Patch"}),
    ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Delete File: /run/synthpulse/secrets/x\n*** End Patch"}),
    ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Move File: /tmp/a -> /run/synthpulse/secrets/x\n*** End Patch"}),
    ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Move File: /run/synthpulse/secrets/x -> /tmp/a\n*** End Patch"}),
)


# ── Every caller, every mode, with and without a context ─────────────────────

@pytest.mark.parametrize("tool_name,args", FILE_CALLS)
def test_file_tools_never_reach_the_secret_mount_without_a_context(tool_name, args):
    decision = tool_arguments_allowed_for_context(None, tool_name, args)
    assert not decision.allowed
    assert decision.reason == REASON


@pytest.mark.parametrize("who", sorted(_all_accesses()))
@pytest.mark.parametrize("tool_name,args", FILE_CALLS)
def test_file_tools_never_reach_the_secret_mount_for_any_caller(who, tool_name, args):
    access = _all_accesses()[who]
    decision = tool_arguments_allowed_for_context(_ctx(access), tool_name, args)
    assert not decision.allowed, (who, tool_name, args)
    assert decision.reason == REASON
    direct = decide_tool_argument_access(access, tool_name, args)
    assert not direct.allowed and direct.reason == REASON


def test_governance_off_context_still_refuses():
    access = EffectiveAccess(subject=GovernanceSubject(email="someone@acme.example"), mode="off")
    assert tool_arguments_allowed_for_context(_ctx(access), "read_file", {"path": SECRET}).reason == REASON
    assert decide_tool_argument_access(access, "read_file", {"path": SECRET}).reason == REASON


def test_relative_traversal_reaches_the_root_and_is_refused(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    depth = len(tmp_path.parts)
    raw = "../" * (depth + 2) + "run/synthpulse/secrets/x"
    assert tool_arguments_allowed_for_context(None, "read_file", {"path": raw}).reason == REASON


def test_relative_path_under_the_terminal_cwd_is_refused(monkeypatch, tmp_path):
    root = tmp_path / "run" / "synthpulse" / "secrets"
    root.mkdir(parents=True)
    monkeypatch.setattr(tool_policy, "SECRET_PATH_ROOTS", (str(root),))
    monkeypatch.setenv("TERMINAL_CWD", str(root.parent))
    decision = tool_arguments_allowed_for_context(None, "read_file", {"path": "secrets/pair.token"})
    assert decision.reason == REASON


def test_a_symlink_into_the_root_is_refused(monkeypatch, tmp_path):
    root = tmp_path / "run" / "synthpulse" / "secrets"
    root.mkdir(parents=True)
    (root / "pair.token").write_text("a2a1.-.hq.inst.secretsecretsecret", encoding="utf-8")
    monkeypatch.setattr(tool_policy, "SECRET_PATH_ROOTS", (str(root),))
    link = tmp_path / "innocent.txt"
    link.symlink_to(root / "pair.token")
    dir_link = tmp_path / "docs"
    dir_link.symlink_to(root)
    for path in (str(link), str(dir_link / "pair.token"), str(dir_link)):
        assert tool_arguments_allowed_for_context(None, "read_file", {"path": path}).reason == REASON, path


def test_a_symlinked_root_is_matched_by_its_target(monkeypatch, tmp_path):
    target = tmp_path / "tmpfs" / "secrets"
    target.mkdir(parents=True)
    root = tmp_path / "run-secrets"
    root.symlink_to(target)
    monkeypatch.setattr(tool_policy, "SECRET_PATH_ROOTS", (str(root),))
    assert tool_arguments_allowed_for_context(None, "read_file", {"path": str(target / "x")}).reason == REASON
    assert tool_arguments_allowed_for_context(None, "read_file", {"path": str(root / "x")}).reason == REASON


@pytest.mark.parametrize("path", (
    "/run/synthpulse/secrets-public/x",
    "/run/synthpulse/other/secrets/x",
    "/run/synthpulse",
    "/tmp/run/synthpulse/secrets/x",
))
def test_neighbouring_paths_are_not_caught(path):
    decision = tool_arguments_allowed_for_context(None, "read_file", {"path": path})
    assert decision.allowed, decision


def test_search_from_an_ancestor_is_refused_only_while_the_root_exists(monkeypatch, tmp_path):
    root = tmp_path / "run" / "synthpulse" / "secrets"
    monkeypatch.setattr(tool_policy, "SECRET_PATH_ROOTS", (str(root),))
    args = {"path": str(tmp_path / "run"), "pattern": "a2a1"}
    # No mount on this host (HQ today): an ordinary search stays allowed.
    assert tool_arguments_allowed_for_context(None, "search_files", args).allowed
    root.mkdir(parents=True)
    decision = tool_arguments_allowed_for_context(None, "search_files", args)
    assert not decision.allowed and decision.reason == REASON
    # A sibling folder is fine; reading the ancestor folder itself is not a search.
    assert tool_arguments_allowed_for_context(None, "search_files", {"path": str(tmp_path / "other")}).allowed


# ── Terminal access is unchanged ─────────────────────────────────────────────

def test_terminal_access_is_unchanged_for_a_terminal_user():
    access = _blacklist_access()
    ctx = _ctx(access)
    assert access.deny == GrantSet()
    assert not access.host_execution_restricted()
    assert tool_allowed_for_context(ctx, "terminal", _Registry()).allowed
    assert tool_allowed_for_context(ctx, "read_file", _Registry()).allowed
    decision = tool_arguments_allowed_for_context(ctx, "terminal", {"command": f"cat {SECRET}"})
    assert decision.allowed, decision
    assert tool_arguments_allowed_for_context(None, "terminal", {"command": f"ls {SECRET}"}).allowed


def test_the_deny_is_not_written_into_the_policy_deny_block():
    access = _blacklist_access()
    tool_arguments_allowed_for_context(_ctx(access), "read_file", {"path": SECRET})
    assert access.deny.file_denied_globs == frozenset()
    assert access.deny.file_read_roots == frozenset()
    assert not access.host_execution_restricted()


def test_ordinary_file_reads_are_unaffected():
    access = _blacklist_access()
    assert tool_arguments_allowed_for_context(_ctx(access), "read_file", {"path": "/tmp/notes.md"}).allowed
    assert tool_arguments_allowed_for_context(None, "read_file", {"path": "/tmp/notes.md"}).allowed
    assert tool_arguments_allowed_for_context(None, "patch", {
        "mode": "patch", "patch": "*** Begin Patch\n*** Update File: /tmp/a.py\n@@\n-a\n+b\n*** End Patch"}).allowed


# ── Through the real dispatcher: nothing of the file reaches the model ──────

@pytest.fixture()
def secret_root(monkeypatch, tmp_path):
    root = tmp_path / "run" / "synthpulse" / "secrets"
    root.mkdir(parents=True)
    (root / "pair.token").write_text("MARKER-a2a1-secret-value\n", encoding="utf-8")
    monkeypatch.setattr(tool_policy, "SECRET_PATH_ROOTS", (str(root),))
    model_tools._clear_tool_defs_cache()
    yield root
    model_tools._clear_tool_defs_cache()


def test_read_file_through_the_dispatcher_without_a_context(secret_root, tmp_path):
    control = tmp_path / "control.txt"
    control.write_text("ordinary-content\n", encoding="utf-8")
    assert "ordinary-content" in model_tools.handle_function_call("read_file", {"path": str(control)})
    out = model_tools.handle_function_call("read_file", {"path": str(secret_root / "pair.token")})
    assert "MARKER" not in out
    result = json.loads(out)
    assert result["error"].startswith(f"Tool denied by dashboard governance: {REASON}")
    assert result["access_request"] == "none"


def test_read_file_through_the_dispatcher_for_the_bootstrap_admin(secret_root):
    access = _bootstrap_access()
    with governance_context(_ctx(access)):
        out = model_tools.handle_function_call("read_file", {"path": str(secret_root / "pair.token")})
    assert "MARKER" not in out
    assert json.loads(out)["governance"]["reason"] == REASON


def test_search_files_through_the_dispatcher_finds_nothing_in_the_mount(secret_root):
    out = model_tools.handle_function_call("search_files", {"path": str(secret_root.parent), "pattern": "MARKER"})
    assert "MARKER-a2a1" not in out
    assert json.loads(out)["governance"]["reason"] == REASON


def test_the_file_tool_recheck_resolves_a_relative_path_against_the_task_cwd(secret_root, monkeypatch):
    """With a governance context the file tools recheck the resolved path."""
    from tools import file_tools

    monkeypatch.setenv("TERMINAL_CWD", str(secret_root.parent))
    access = _blacklist_access()
    with governance_context(_ctx(access)):
        blocked = file_tools._check_governance_path("secrets/pair.token", "read")
    assert blocked is not None and REASON in blocked
