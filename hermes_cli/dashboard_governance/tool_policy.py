from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .context import DashboardGovernanceContext
from .models import AccessDecision, EffectiveAccess

_SHELL_OPERATOR_RE = re.compile(r"(;|&&|\|\||\||`|\$\(|<\(|>\(|\s[<>]{1,2}\s|\d[<>])")


@dataclass(frozen=True)
class ToolIdentity:
    name: str
    toolset: str = ""
    mcp_server: str = ""
    mcp_tool: str = ""


def _contains(values: frozenset[str], value: str) -> bool:
    return "*" in values or bool(value and value in values)


def _entry_toolset(registry: Any, tool_name: str) -> str:
    try:
        entry = registry.get_entry(tool_name)
        toolset = getattr(entry, "toolset", "") if entry is not None else ""
        if toolset:
            return str(toolset)
    except Exception:
        pass
    try:
        return str(registry.get_toolset_for_tool(tool_name) or "")
    except Exception:
        return ""


def identify_tool(tool_name: str, registry: Any) -> ToolIdentity:
    toolset = _entry_toolset(registry, tool_name)
    if toolset.startswith("mcp-"):
        server = toolset[4:]
        local = tool_name
        prefix = f"mcp_{server}_"
        if tool_name.startswith(prefix):
            local = tool_name[len(prefix):]
        return ToolIdentity(name=tool_name, toolset=toolset, mcp_server=server, mcp_tool=local)
    return ToolIdentity(name=tool_name, toolset=toolset)


def _mcp_tool_allowed(access: EffectiveAccess, identity: ToolIdentity) -> bool:
    grants = access.grants
    server_keys = {identity.mcp_server, identity.toolset}
    server_allowed = "*" in grants.mcp_servers or any(key and key in grants.mcp_servers for key in server_keys)
    if not server_allowed:
        return False
    for key in ("*", identity.mcp_server, identity.toolset):
        allowed_names = grants.mcp_tools.get(key)
        if allowed_names and ("*" in allowed_names or identity.name in allowed_names or identity.mcp_tool in allowed_names):
            return True
    return False


def decide_tool_access(access: EffectiveAccess | None, tool_name: str, registry: Any) -> AccessDecision:
    """Return whether *tool_name* may be exposed/executed for access.

    Modes:
    - no context/off/report_only: allow for compatibility; report_only auditing
      can be layered later without hiding schemas or blocking calls.
    - enforce: require explicit tool, toolset or MCP grant.
    """
    if access is None or access.mode != "enforce":
        return AccessDecision(True, "governance_inactive")
    identity = identify_tool(tool_name, registry)
    grants = access.grants
    if _contains(grants.tools, tool_name):
        if identity.mcp_server and not _mcp_tool_allowed(access, identity):
            return AccessDecision(False, "tool_not_allowed")
        return AccessDecision(True, "tool_allowed")
    if identity.mcp_server:
        if _mcp_tool_allowed(access, identity):
            return AccessDecision(True, "mcp_tool_allowed")
        return AccessDecision(False, "tool_not_allowed")
    if identity.toolset and _contains(grants.toolsets, identity.toolset):
        return AccessDecision(True, "toolset_allowed")
    return AccessDecision(False, "tool_not_allowed")


def tool_allowed_for_context(ctx: DashboardGovernanceContext | None, tool_name: str, registry: Any) -> AccessDecision:
    return decide_tool_access(ctx.access if ctx is not None else None, tool_name, registry)


def _resolve_candidate_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(os.path.expanduser(raw)).resolve(strict=False))
    except Exception:
        return os.path.abspath(os.path.expanduser(raw))


def _path_within_roots(path: str, roots: frozenset[str]) -> bool:
    if not path or not roots or "*" in roots:
        return True
    candidate = _resolve_candidate_path(path)
    for root in roots:
        root_path = _resolve_candidate_path(root)
        if root_path and (candidate == root_path or candidate.startswith(root_path.rstrip(os.sep) + os.sep)):
            return True
    return False


def _matches_denied_glob(path: str, globs: frozenset[str]) -> bool:
    if not path or not globs:
        return False
    candidate = _resolve_candidate_path(path)
    raw = str(path)
    return any(fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(raw, pattern) for pattern in globs)


def _command_id(command: Any) -> tuple[str, str]:
    raw = str(command or "").strip()
    if not raw:
        return "", ""
    try:
        parts = shlex.split(raw, posix=True)
    except ValueError:
        parts = raw.split()
    if not parts:
        return "", ""
    argv0 = parts[0]
    return argv0, os.path.basename(argv0)


def _skill_name_allowed(values: frozenset[str], name: Any) -> bool:
    skill = str(name or "").strip()
    return bool(skill) and ("*" in values or skill in values)


def decide_tool_argument_access(access: EffectiveAccess | None, tool_name: str, args: dict[str, Any]) -> AccessDecision:
    if access is None or access.mode != "enforce":
        return AccessDecision(True, "governance_inactive")
    grants = access.grants
    if tool_name == "skill_view":
        if not _skill_name_allowed(grants.skills_view, args.get("name")):
            return AccessDecision(False, "skill_not_allowed")
    elif tool_name == "skill_manage":
        if not _skill_name_allowed(grants.skills_manage, args.get("name")):
            return AccessDecision(False, "skill_manage_not_allowed")
    elif tool_name in {"read_file", "search_files"}:
        path = args.get("path") or "."
        if _matches_denied_glob(str(path), grants.file_denied_globs):
            return AccessDecision(False, "file_denied_glob")
        if grants.file_read_roots and not _path_within_roots(str(path), grants.file_read_roots):
            return AccessDecision(False, "file_read_root_not_allowed")
    elif tool_name in {"write_file", "patch"}:
        path = args.get("path")
        if path:
            if _matches_denied_glob(str(path), grants.file_denied_globs):
                return AccessDecision(False, "file_denied_glob")
            if grants.file_write_roots and not _path_within_roots(str(path), grants.file_write_roots):
                return AccessDecision(False, "file_write_root_not_allowed")
    elif tool_name == "terminal":
        command = args.get("command")
        if _SHELL_OPERATOR_RE.search(str(command or "")):
            return AccessDecision(False, "cli_shell_operator_not_allowed")
        argv0, basename = _command_id(command)
        if grants.cli_commands and "*" not in grants.cli_commands:
            if argv0 not in grants.cli_commands and basename not in grants.cli_commands:
                return AccessDecision(False, "cli_command_not_allowed")
        workdir = args.get("workdir")
        if workdir and grants.cli_workdir_roots and not _path_within_roots(str(workdir), grants.cli_workdir_roots):
            return AccessDecision(False, "cli_workdir_not_allowed")
    return AccessDecision(True, "arguments_allowed")


def tool_arguments_allowed_for_context(ctx: DashboardGovernanceContext | None, tool_name: str, args: dict[str, Any]) -> AccessDecision:
    return decide_tool_argument_access(ctx.access if ctx is not None else None, tool_name, args)
