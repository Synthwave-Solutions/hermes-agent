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

# Backticks and process substitution hide a nested command from segment
# parsing, so they stay blocked outright for governed users. Plain operators
# (;, &&, ||, |) are fine: every segment's argv0 is checked against the CLI
# allowlist below. Command substitution $(...) is NOT hard-blocked anymore:
# its inner command is extracted and segment-checked recursively (25-08-2026,
# to stop routine agent patterns like $(date +%F) failing for governed users).
_SHELL_SUBSTITUTION_RE = re.compile(r"(`|<\(|>\()")

_CMD_SUBSTITUTION_MARK = "__HERMES_SUBST__"


def _extract_cmd_substitutions(command: str) -> tuple[str, list[str]]:
    """Replace every balanced $(...) span with a placeholder and return the
    rewritten command plus the extracted inner commands (outermost level;
    nested substitutions stay inside the inner string and are handled by the
    recursive check). Raises ValueError on unbalanced parentheses."""
    out: list[str] = []
    inners: list[str] = []
    i, n = 0, len(command)
    while i < n:
        if command[i] == "$" and i + 1 < n and command[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < n and depth:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            if depth:
                raise ValueError("unbalanced command substitution")
            inners.append(command[i + 2:j - 1])
            out.append(_CMD_SUBSTITUTION_MARK)
            i = j
        else:
            out.append(command[i])
            i += 1
    return "".join(out), inners

# Shell builtins that carry no execution surface of their own; they may appear
# as a segment head without an allowlist entry (export CLOUDSDK_CONFIG=...; ...).
_SHELL_BUILTINS = frozenset({
    "export", "cd", "set", "unset", "true", "false", "test", "[", "[[", "pwd",
    "wait", "exit", "read", "umask", "ulimit", "echo", "printf",
    # Shell keywords/control flow: no execution surface of their own; the
    # commands inside the construct are still segment-checked individually.
    "for", "while", "until", "do", "done", "if", "then", "else", "elif", "fi",
    "case", "esac", "select", "in", "time", "{", "}", "!", "break", "continue",
    "return", "local", "declare", "shift",
})

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT_TOKEN_RE = re.compile(r"^\d*(>>|>|<|>&|<&|&>>|&>)\d*$")


def _split_shell_segments(command: str) -> list[list[str]]:
    """Split a shell command on ;, &&, ||, |, & and newlines into token lists,
    respecting quoting. Raises ValueError on unparseable input."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=";|&")
    lex.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lex:
        if token in {";", "|", "||", "&&", "&", ";;", "|&"}:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    return [seg for seg in segments if seg]


def _segment_argv0(tokens: list[str]) -> str:
    """First real command word of a segment: skips VAR=val prefixes,
    redirection operators plus their targets, and an `env` prefix."""
    skip_next = False
    saw_env = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if _REDIRECT_TOKEN_RE.match(token):
            skip_next = True
            continue
        if _ENV_ASSIGNMENT_RE.match(token):
            continue
        if token == "env" and not saw_env:
            saw_env = True
            continue
        return token
    return ""


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
    if not skill:
        return False
    if "*" in values or skill in values:
        return True
    # Skill trees expose category-prefixed names ("synthwave/opencode",
    # "autonomous-ai-agents/opencode"); grants list bare names. Match on the
    # final path segment so one grant covers every category alias.
    return skill.rsplit("/", 1)[-1] in values


def _check_cli_command(command_s: str, grants) -> AccessDecision:
    """Validate one shell command string against the CLI grants: hard-block
    backticks/process substitution, recursively validate $(...) contents, and
    check every segment's argv0 against the allowlist."""
    if _SHELL_SUBSTITUTION_RE.search(command_s):
        return AccessDecision(False, "cli_shell_operator_not_allowed")
    try:
        command_s, inners = _extract_cmd_substitutions(command_s)
    except ValueError:
        return AccessDecision(False, "cli_shell_operator_not_allowed")
    for inner in inners:
        inner_decision = _check_cli_command(inner, grants)
        if not inner_decision.allowed:
            return inner_decision
    if grants.cli_commands and "*" not in grants.cli_commands:
        try:
            segments = _split_shell_segments(command_s)
        except ValueError:
            return AccessDecision(False, "cli_shell_operator_not_allowed")
        if not segments:
            return AccessDecision(False, "cli_command_not_allowed")
        for tokens in segments:
            seg_argv0 = _segment_argv0(tokens)
            seg_base = os.path.basename(seg_argv0) if seg_argv0 else ""
            if not seg_argv0 or seg_argv0 == _CMD_SUBSTITUTION_MARK:
                continue
            if seg_argv0 in _SHELL_BUILTINS or seg_base in _SHELL_BUILTINS:
                continue
            if seg_argv0 not in grants.cli_commands and seg_base not in grants.cli_commands:
                return AccessDecision(False, "cli_command_not_allowed", detail=seg_argv0)
    return AccessDecision(True, "arguments_allowed")


def decide_tool_argument_access(access: EffectiveAccess | None, tool_name: str, args: dict[str, Any]) -> AccessDecision:
    if access is None or access.mode != "enforce":
        return AccessDecision(True, "governance_inactive")
    grants = access.grants
    if tool_name == "skill_view":
        if not _skill_name_allowed(grants.skills_view, args.get("name")):
            return AccessDecision(False, "skill_not_allowed", detail=str(args.get("name") or ""))
    elif tool_name == "skill_manage":
        if not _skill_name_allowed(grants.skills_manage, args.get("name")):
            return AccessDecision(False, "skill_manage_not_allowed", detail=str(args.get("name") or ""))
    elif tool_name in {"read_file", "search_files"}:
        path = args.get("path") or "."
        if _matches_denied_glob(str(path), grants.file_denied_globs):
            return AccessDecision(False, "file_denied_glob", detail=str(path))
        if grants.file_read_roots and not _path_within_roots(str(path), grants.file_read_roots):
            return AccessDecision(False, "file_read_root_not_allowed", detail=str(path))
    elif tool_name in {"write_file", "patch"}:
        path = args.get("path")
        if path:
            if _matches_denied_glob(str(path), grants.file_denied_globs):
                return AccessDecision(False, "file_denied_glob", detail=str(path))
            if grants.file_write_roots and not _path_within_roots(str(path), grants.file_write_roots):
                return AccessDecision(False, "file_write_root_not_allowed", detail=str(path))
    elif tool_name == "terminal":
        command = args.get("command")
        command_s = str(command or "")
        decision = _check_cli_command(command_s, grants)
        if not decision.allowed:
            return decision
        workdir = args.get("workdir")
        if workdir and grants.cli_workdir_roots and not _path_within_roots(str(workdir), grants.cli_workdir_roots):
            return AccessDecision(False, "cli_workdir_not_allowed", detail=str(workdir))
    return AccessDecision(True, "arguments_allowed")


def tool_arguments_allowed_for_context(ctx: DashboardGovernanceContext | None, tool_name: str, args: dict[str, Any]) -> AccessDecision:
    return decide_tool_argument_access(ctx.access if ctx is not None else None, tool_name, args)
