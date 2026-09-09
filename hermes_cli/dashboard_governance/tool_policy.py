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

# Domain-wide-delegation CLIs. With `--as <email>` they act as ANY
# @synthwave.solutions mailbox; without it they run on the owner's own OAuth
# token (reads AND sends land as Michael). Hard requirement 29-08-2026: a
# governed non-admin may only drive their OWN account, so every call to one
# of these must carry `--as <their identity email>`; a missing or different
# subject is refused. Enforced here (not via grants) so a grant rewrite can
# never widen it: adding a role to the exempt set below takes a commit and a
# review, which is the point. Admins (bootstrap admins, owner/admin role) are
# exempt. admin_delivery joined them on 02-09-2026 on Michael's explicit
# decision, asked as "net zoals wij als admins": a delivery lead reads every
# colleague's mail, Chat and calendar, and sends as them too.
_DWD_CLIS = frozenset({"gchat", "gmail", "gws-hermes", "gdrive-dwd", "gdrive_dwd.py"})
_DWD_ADMIN_ROLES = frozenset({"owner", "admin", "admin_delivery"})
# Sentinel: no identity binding for this caller (admin or governance off).
_DWD_UNRESTRICTED = None


def dwd_identity_for(access: EffectiveAccess | None):
    """The identity a caller's DWD CLI calls must be bound to, or
    _DWD_UNRESTRICTED for admins. An unknown email binds to '' (fail closed)."""
    if access is None:
        return _DWD_UNRESTRICTED
    if "bootstrap_admin" in tuple(access.grant_sources or ()):
        return _DWD_UNRESTRICTED
    if frozenset(access.roles or ()) & _DWD_ADMIN_ROLES:
        return _DWD_UNRESTRICTED
    email = getattr(access.subject, "email", "") or ""
    return str(email).strip().lower()


def _dwd_subject(tokens: list[str]) -> str:
    """The `--as` subject in one segment ('' when absent)."""
    for i, tok in enumerate(tokens):
        if tok == "--as":
            return tokens[i + 1].strip().lower() if i + 1 < len(tokens) else ""
        if tok.startswith("--as="):
            return tok[len("--as="):].strip().lower()
    return ""


def _check_dwd_identity(segments: list[list[str]], identity: str) -> AccessDecision:
    for tokens in segments:
        argv0 = _segment_argv0(tokens)
        base = os.path.basename(argv0) if argv0 else ""
        if base not in _DWD_CLIS:
            continue
        subject = _dwd_subject(tokens)
        if not subject:
            return AccessDecision(False, "dwd_identity_required", detail=base)
        if not identity or subject != identity:
            return AccessDecision(False, "dwd_identity_mismatch", detail=f"{base} --as {subject}")
    return AccessDecision(True, "arguments_allowed")


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

# Heredoc opener: << or <<- plus a delimiter word, quoted or bare. The
# lookarounds keep <<< (herestring) out, and the trailing check keeps a stray
# "a << b" inside a quoted string from being read as one.
_HEREDOC_OPEN_RE = re.compile(
    r"(?<!<)<<(-?)(?!<)\s*(?:([\'\"])([A-Za-z_][A-Za-z0-9_]*)\2|([A-Za-z_][A-Za-z0-9_]*))(?=\s|$)"
)


def _heredoc_openers(line: str, quote: str) -> tuple[list[Any], str]:
    """Locate real redirection headers, not quoted or commented examples."""
    matches = []
    index = 0
    word_start = True
    while index < len(line):
        char = line[index]
        if char == "\\" and quote != "'":
            index += 2
            word_start = False
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            word_start = False
        elif char == "#" and word_start:
            break
        else:
            match = _HEREDOC_OPEN_RE.match(line, index)
            if match:
                matches.append(match)
                index = match.end()
                word_start = False
                continue
            word_start = char.isspace() or char in ";|&()<>"
        index += 1
    return matches, quote


def _strip_heredocs(command: str) -> tuple[str, list[str]]:
    """Remove heredoc bodies before segmentation, returning the remaining
    command plus the bodies the shell would still expand.

    A heredoc body is stdin DATA for a command word that is itself checked, not
    a list of commands: `python3 - <<'PY' ... PY` is one python3 call. Without
    this, any ; | && inside the script split the body into fake segments whose
    first word was matched against cli.commands and denied, so an ordinary
    python heredoc failed for every governed user. Reported by Hrishikesh
    Oemraw on 28 Aug 2026 ("everything I ask gives governance"), on
    `python3 - <<'PY' ... d=json.load(r); print('URL',u) ... PY`, denied as
    cli_command_not_allowed (print(URL,u)).

    This grants nothing new: `python3 -c '<anything>'` was already opaque to
    the gate. A BARE delimiter (<<PY) is still expanded by the shell, so those
    bodies are handed back for the caller's substitution checks; a quoted
    delimiter (<<'PY') is literal and is dropped.
    """
    if "<<" not in command:
        return command, []
    lines = command.split("\n")
    kept: list[str] = []
    expanded: list[str] = []
    index = 0
    quote = ""
    while index < len(lines):
        line = lines[index]
        index += 1
        matches, quote = _heredoc_openers(line, quote)
        openers = [
            (match.group(3) or match.group(4), match.group(2) is None, match.group(1) == "-")
            for match in matches
        ]
        for match in reversed(matches):
            line = line[:match.start()] + " " + line[match.end():]
        kept.append(line)
        for delimiter, expands, strip_tabs in openers:
            body: list[str] = []
            while index < len(lines):
                current = lines[index]
                if current.strip() == delimiter or (strip_tabs and current.lstrip("\t") == delimiter):
                    index += 1
                    break
                body.append(current)
                index += 1
            if expands:
                expanded.append("\n".join(body))
    return "\n".join(kept), expanded


# Shell builtins that carry no execution surface of their own; they may appear
# as a segment head without an allowlist entry (export CLOUDSDK_CONFIG=...; ...).
_SHELL_BUILTINS = frozenset({
    "export", "cd", "set", "unset", "true", "false", "test", "[", "[[", "pwd",
    "wait", "exit", "read", "umask", "ulimit", "echo", "printf",
    # Compound syntax is recognized separately and refused when command
    # constraints apply; these names never grant its nested executables.
    "for", "while", "until", "do", "done", "if", "then", "else", "elif", "fi",
    "case", "esac", "select", "in", "time", "{", "}", "!", "break", "continue",
    "return", "local", "declare", "shift",
})

_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT_TOKEN_RE = re.compile(r"^\d*(>>|>|<|>&|<&|&>>|&>)\d*$")


def _strip_shell_comments(command: str) -> str:
    """Remove shell comments without losing the newline command boundary.

    shlex's built-in comment handling consumes that newline, merging the
    next executable into the previous command's arguments. Quoted/escaped
    hashes and hashes inside a word are ordinary data and must survive.
    """
    out = []
    quote = ""
    word_start = True
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            out.append(command[index:index + 2])
            if command[index + 1] != "\n":
                word_start = False
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            word_start = False
        elif char == "#" and word_start:
            index = command.find("\n", index)
            if index < 0:
                break
            continue
        else:
            word_start = char.isspace() or char in ";|&()<>"
        out.append(char)
        index += 1
    return "".join(out)


def _split_shell_segments(command: str) -> list[list[str]]:
    """Split a shell command on ;, &&, ||, |, & and newlines into token lists,
    respecting quoting. Raises ValueError on unparseable input."""
    lex = shlex.shlex(_strip_shell_comments(command), posix=True, punctuation_chars=";|&\n")
    lex.commenters = ""
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lex:
        if token and all(char in ";|&\n" for char in token):
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    return [seg for seg in segments if seg]


# Words that introduce another command without consuming arguments of their
# own. Without this, `command gchat --as someone-else` read as a call to
# `command` and slipped past both the allowlist and the identity binding.
_COMMAND_WRAPPERS = frozenset({"command", "builtin", "exec", "nohup", "time"})
_SHELL_CONTROL_HEADS = frozenset({
    "for", "while", "until", "do", "done", "if", "then", "else", "elif", "fi",
    "case", "esac", "select", "in", "function", "coproc", "{", "}", "!",
})


def _segment_argv0(tokens: list[str]) -> str:
    """First real command word of a segment: skips VAR=val prefixes,
    redirection operators plus their targets, an `env` prefix, and wrapper
    words such as `command` that just introduce the real command."""
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
        if token in _COMMAND_WRAPPERS:
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
        # Native registration uses mcp__<sanitized server>__<tool>. Keep
        # legacy names readable too, but derive the server from the trusted
        # registry toolset rather than guessing at underscore boundaries.
        safe_server = re.sub(r"[^A-Za-z0-9_]", "_", server)
        for prefix in (f"mcp__{safe_server}__", f"mcp_{server}_", f"mcp_{safe_server}_"):
            if tool_name.startswith(prefix):
                local = tool_name[len(prefix):]
                break
        return ToolIdentity(name=tool_name, toolset=toolset, mcp_server=server, mcp_tool=local)
    return ToolIdentity(name=tool_name, toolset=toolset)


def _mcp_tool_allowed(access: EffectiveAccess, identity: ToolIdentity) -> bool:
    from .models import grant_matches
    grants = access.grants
    server_keys = {identity.mcp_server, identity.toolset}
    if any(key and grant_matches(access.deny.mcp_servers, key) for key in server_keys):
        return False
    for key in ("*", identity.mcp_server, identity.toolset):
        denied = access.deny.mcp_tools.get(key, frozenset())
        if grant_matches(denied, identity.name) or grant_matches(denied, identity.mcp_tool):
            return False
    server_allowed = any(key and access.allows("mcp_servers", key) for key in server_keys)
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
    from .models import grant_matches
    if grant_matches(access.deny.tools, tool_name) or (identity.toolset and grant_matches(access.deny.toolsets, identity.toolset)):
        return AccessDecision(False, "explicit_deny", detail=tool_name)
    if (access.access_mode or access.access_level) and (access.access_level or "user") == "user" and tool_name in {"terminal", "execute_code"}:
        return AccessDecision(False, "access_level_not_allowed", detail=tool_name)
    if access.host_execution_restricted() and tool_name in {"terminal", "execute_code"}:
        # A host process can derive paths/environment names internally. Human
        # and AI review cannot turn string inspection into process isolation.
        return AccessDecision(False, "host_execution_conflicts_with_resource_deny", detail=tool_name)
    if access.allows("tools", tool_name):
        if identity.mcp_server and not _mcp_tool_allowed(access, identity):
            return AccessDecision(False, "tool_not_allowed")
        return AccessDecision(True, "tool_allowed")
    if identity.mcp_server:
        if _mcp_tool_allowed(access, identity):
            return AccessDecision(True, "mcp_tool_allowed")
        return AccessDecision(False, "tool_not_allowed")
    if identity.toolset and access.allows("toolsets", identity.toolset):
        return AccessDecision(True, "toolset_allowed")
    return AccessDecision(False, "tool_not_allowed")


def tool_allowed_for_context(ctx: DashboardGovernanceContext | None, tool_name: str, registry: Any) -> AccessDecision:
    from .context import policy_contexts
    try:
        contexts = policy_contexts(ctx)
        first = AccessDecision(True, "governance_inactive")
        for index, bounded_ctx in enumerate(contexts):
            if index and bounded_ctx.access.mode == "enforce" and not bounded_ctx.access.is_profile_allowed(bounded_ctx.active_profile):
                return AccessDecision(False, "profile_not_allowed")
            if index and bounded_ctx.access.mode == "enforce" and (bounded_ctx.access.access_mode or bounded_ctx.access.access_level) and not bounded_ctx.access.has_permission("chat:use"):
                return AccessDecision(False, "chat_not_allowed")
            decision = _tool_allowed_for_single_context(bounded_ctx, tool_name, registry)
            if not decision.allowed:
                return decision
            if index == 0:
                first = decision
        return first
    except Exception:
        return AccessDecision(False, "continuation_policy_unavailable")


def _tool_allowed_for_single_context(ctx, tool_name, registry):
    from .context import workspace_allowed_for_context
    if not workspace_allowed_for_context(ctx):
        return AccessDecision(False, "workspace_access_revoked")
    ceiling = getattr(ctx, "bot_access_ceiling", None)
    if ceiling is not None:
        checker = getattr(ctx, "bot_access_check", None)
        try:
            if not callable(checker) or checker() is not True:
                return AccessDecision(False, "bot_access_revoked")
        except Exception:
            return AccessDecision(False, "bot_access_revoked")
        if tool_name == "terminal" and not ceiling.grants.cli_commands:
            return AccessDecision(False, "bot_cli_not_selected")
        bounded = decide_tool_access(ceiling, tool_name, registry)
        if not bounded.allowed:
            return bounded
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


# A path anywhere in a command line, quoted or not: `cat ~/.hermes/x.json`,
# `python3 -c "open('/home/.../key.json')"`, `cp a b` all expose one.
_PATHLIKE_RE = re.compile(r"(?:~|\.{0,2}/)[A-Za-z0-9._~/@+-]*")


def _check_denied_paths(command_s: str, grants) -> AccessDecision:
    """Apply the file denied-globs to shell commands too.

    The read_file/write_file tools honoured denied_globs, the terminal did
    not, so `cat ~/.hermes/gmail-dwd-sa.json` handed a governed user the
    domain-wide-delegation key that the file tools refused them (found
    29-08-2026). One path per denial keeps the message actionable, and the
    per-person allow_globs exception still opens a single file.
    """
    if not grants.file_denied_globs:
        return AccessDecision(True, "arguments_allowed")
    for candidate in _PATHLIKE_RE.findall(command_s):
        if len(candidate) < 2:
            continue
        if _matches_denied_glob(candidate, grants.file_denied_globs) \
                and not _matches_denied_glob(candidate, grants.file_allow_globs):
            return AccessDecision(False, "file_denied_glob", detail=candidate)
    return AccessDecision(True, "arguments_allowed")


def _check_identity_env_tamper(command_s: str) -> AccessDecision:
    """Refuse any attempt to touch the identity the child processes carry.

    HERMES_DWD_IDENTITY is what pins the Google CLIs to the caller's own
    account. Unsetting or rewriting it is never a legitimate need, and naming
    it at all is the only way to try.
    """
    if "HERMES_DWD_IDENTITY" in command_s:
        return AccessDecision(False, "dwd_identity_tamper", detail="HERMES_DWD_IDENTITY")
    return AccessDecision(True, "arguments_allowed")


def _check_expanded_heredoc_body(body: str, grants, dwd_identity=_DWD_UNRESTRICTED) -> AccessDecision:
    """A bare-delimiter heredoc is still expanded by the shell, so anything the
    shell would RUN inside it stays gated: backticks and process substitution
    are refused, and each $(...) is checked as a command. The literal text
    itself is data and is not segmented."""
    if _SHELL_SUBSTITUTION_RE.search(body):
        return AccessDecision(False, "cli_shell_operator_not_allowed")
    try:
        _, inners = _extract_cmd_substitutions(body)
    except ValueError:
        return AccessDecision(False, "cli_shell_operator_not_allowed")
    for inner in inners:
        inner_decision = _check_cli_command(inner, grants, dwd_identity)
        if not inner_decision.allowed:
            return inner_decision
    return AccessDecision(True, "arguments_allowed")


def _check_cli_command(command_s: str, grants, dwd_identity=_DWD_UNRESTRICTED) -> AccessDecision:
    """Validate one shell command string against the CLI grants: hard-block
    backticks/process substitution, recursively validate $(...) contents, and
    check every segment's argv0 against the allowlist."""
    command_s, heredoc_bodies = _strip_heredocs(command_s)
    for body in heredoc_bodies:
        body_decision = _check_expanded_heredoc_body(body, grants, dwd_identity)
        if not body_decision.allowed:
            return body_decision
    if _SHELL_SUBSTITUTION_RE.search(command_s):
        return AccessDecision(False, "cli_shell_operator_not_allowed")
    try:
        command_s, inners = _extract_cmd_substitutions(command_s)
    except ValueError:
        return AccessDecision(False, "cli_shell_operator_not_allowed")
    for inner in inners:
        inner_decision = _check_cli_command(inner, grants, dwd_identity)
        if not inner_decision.allowed:
            return inner_decision
    denied_path = _check_denied_paths(command_s, grants)
    if not denied_path.allowed:
        return denied_path
    if grants.cli_denied_commands or (grants.cli_commands and "*" not in grants.cli_commands):
        # shlex identifies ordinary command chains; it is not a shell AST.
        # A compound head can hide a second executable in the same segment.
        # With hard command constraints, unknown structure must remain denied.
        # The review-only caller uses this same result to require a human.
        try:
            segments = _split_shell_segments(command_s)
        except ValueError:
            return AccessDecision(False, "cli_shell_operator_not_allowed")
        for tokens in segments:
            head = _segment_argv0(tokens)
            if head in _SHELL_CONTROL_HEADS or any(char in head for char in "(){}"):
                return AccessDecision(False, "cli_compound_command_not_allowed")
    if dwd_identity is not _DWD_UNRESTRICTED:
        tamper = _check_identity_env_tamper(command_s)
        if not tamper.allowed:
            return tamper
        # Identity binding runs regardless of the allowlist: even a wildcard
        # CLI grant must not let a non-admin act as someone else.
        try:
            dwd_segments = _split_shell_segments(command_s)
        except ValueError:
            return AccessDecision(False, "cli_shell_operator_not_allowed")
        dwd_decision = _check_dwd_identity(dwd_segments, dwd_identity)
        if not dwd_decision.allowed:
            return dwd_decision
    # A per-person CLI deny is subtracted into cli_denied_commands and has to
    # bite even when the role hands out a wildcard, otherwise the only way to
    # keep one command away from an administrator is to take the wildcard back
    # and enumerate every other command. Checked before the allowlist so deny
    # wins, and independently of it so "*" does not skip past it.
    if grants.cli_denied_commands:
        try:
            denied_segments = _split_shell_segments(command_s)
        except ValueError:
            return AccessDecision(False, "cli_shell_operator_not_allowed")
        for tokens in denied_segments:
            seg_argv0 = _segment_argv0(tokens)
            seg_base = os.path.basename(seg_argv0) if seg_argv0 else ""
            if not seg_argv0 or seg_argv0 == _CMD_SUBSTITUTION_MARK:
                continue
            from .models import grant_matches
            if grant_matches(grants.cli_denied_commands, seg_argv0) or grant_matches(grants.cli_denied_commands, seg_base):
                return AccessDecision(False, "cli_command_denied", detail=seg_argv0)
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


def cli_command_requires_manual_approval(access: EffectiveAccess, command: str) -> bool:
    """Use the command permission parser for the current policy's review floor.

    This is a review requirement, never a permission grant. Any matching
    executable (including a nested substitution) or unsupported parse needs
    a human; the caller must still perform the full hard checks first.
    """
    from .models import GrantSet
    required = access.grants.cli_approval_commands
    if not required or "bootstrap_admin" in access.grant_sources:
        return False
    review_selectors = GrantSet(cli_denied_commands=required)
    return not _check_cli_command(command, review_selectors).allowed


def decide_tool_argument_access(access: EffectiveAccess | None, tool_name: str, args: dict[str, Any]) -> AccessDecision:
    if access is None or access.mode != "enforce":
        return AccessDecision(True, "governance_inactive")
    grants = access.grants
    from .models import grant_matches
    if tool_name in {"skill_view", "skill_manage"}:
        dim = "skills_view" if tool_name == "skill_view" else "skills_manage"
        name = str(args.get("name") or "")
        if grant_matches(getattr(access.deny, dim), name) or grant_matches(getattr(access.deny, dim), name.rsplit("/", 1)[-1]):
            return AccessDecision(False, "explicit_deny", detail=name)
        if tool_name == "skill_view" and (grant_matches(access.deny.skills_load, name) or
                                          grant_matches(access.deny.skills_load, name.rsplit("/", 1)[-1])):
            return AccessDecision(False, "explicit_deny", detail=name)
        if (access.access_mode or access.access_level) and not access.allows(dim, name):
            return AccessDecision(False, "skill_not_allowed", detail=name)
    if tool_name in {"read_file", "search_files", "write_file", "patch"}:
        path = str(args.get("path") or ".")
        canonical = _resolve_candidate_path(path)
        dim = "file_read_roots" if tool_name in {"read_file", "search_files"} else "file_write_roots"
        if (_matches_denied_glob(path, access.deny.file_denied_globs)
                or _matches_denied_glob(canonical, access.deny.file_denied_globs)
                or grant_matches(getattr(access.deny, dim), canonical, path=True)):
            return AccessDecision(False, "explicit_deny", detail=path)
        if (access.access_mode or access.access_level) and not access.allows(dim, canonical, path=True):
            return AccessDecision(False, "file_root_not_allowed", detail=path)
    if tool_name == "terminal" and (access.access_mode or access.access_level):
        if not grants.cli_commands:
            return AccessDecision(False, "cli_command_not_allowed")
        workdir = _resolve_candidate_path(args.get("workdir") or ".")
        if not access.allows("cli_workdir_roots", workdir, path=True):
            return AccessDecision(False, "cli_workdir_not_allowed", detail=workdir)
        if not access.has_permission("terminal:use"):
            return AccessDecision(False, "terminal_not_allowed")
        for candidate in _PATHLIKE_RE.findall(str(args.get("command") or "")):
            canonical = _resolve_candidate_path(candidate)
            if (grant_matches(access.deny.file_read_roots | access.deny.file_write_roots, canonical, path=True)
                    or _matches_denied_glob(candidate, access.deny.file_denied_globs)
                    or _matches_denied_glob(canonical, access.deny.file_denied_globs)):
                return AccessDecision(False, "explicit_deny", detail=candidate)
    if tool_name == "skill_view":
        if not _skill_name_allowed(grants.skills_view, args.get("name")):
            return AccessDecision(False, "skill_not_allowed", detail=str(args.get("name") or ""))
    elif tool_name == "skill_manage":
        if not _skill_name_allowed(grants.skills_manage, args.get("name")):
            return AccessDecision(False, "skill_manage_not_allowed", detail=str(args.get("name") or ""))
    elif tool_name in {"read_file", "search_files"}:
        path = args.get("path") or "."
        if _matches_denied_glob(str(path), grants.file_denied_globs) \
                and not _matches_denied_glob(str(path), grants.file_allow_globs):
            return AccessDecision(False, "file_denied_glob", detail=str(path))
        if grants.file_read_roots and not _path_within_roots(str(path), grants.file_read_roots):
            return AccessDecision(False, "file_read_root_not_allowed", detail=str(path))
    elif tool_name in {"write_file", "patch"}:
        path = args.get("path")
        if path:
            if _matches_denied_glob(str(path), grants.file_denied_globs) \
                    and not _matches_denied_glob(str(path), grants.file_allow_globs):
                return AccessDecision(False, "file_denied_glob", detail=str(path))
            if grants.file_write_roots and not _path_within_roots(str(path), grants.file_write_roots):
                return AccessDecision(False, "file_write_root_not_allowed", detail=str(path))
    elif tool_name == "terminal":
        command = args.get("command")
        command_s = str(command or "")
        decision = _check_cli_command(command_s, grants, dwd_identity_for(access))
        if not decision.allowed:
            return decision
        workdir = args.get("workdir")
        if workdir and grants.cli_workdir_roots and not _path_within_roots(str(workdir), grants.cli_workdir_roots):
            return AccessDecision(False, "cli_workdir_not_allowed", detail=str(workdir))
    return AccessDecision(True, "arguments_allowed")


def tool_arguments_allowed_for_context(ctx: DashboardGovernanceContext | None, tool_name: str, args: dict[str, Any]) -> AccessDecision:
    from .context import policy_contexts
    try:
        contexts = policy_contexts(ctx)
        first = AccessDecision(True, "governance_inactive")
        for index, bounded_ctx in enumerate(contexts):
            decision = _tool_arguments_for_single_context(bounded_ctx, tool_name, args)
            if not decision.allowed:
                return decision
            if index == 0:
                first = decision
        return first
    except Exception:
        return AccessDecision(False, "continuation_policy_unavailable")


def _tool_arguments_for_single_context(ctx, tool_name, args):
    from .context import workspace_allowed_for_context
    candidate = ""
    if tool_name in {"read_file", "search_files", "write_file", "patch"}:
        candidate = _resolve_candidate_path(args.get("path") or ".")
    elif tool_name == "terminal":
        candidate = _resolve_candidate_path(args.get("workdir") or getattr(ctx, "workspace_path", "") or ".")
    if not workspace_allowed_for_context(ctx, candidate):
        return AccessDecision(False, "workspace_access_revoked")
    ceiling = getattr(ctx, "bot_access_ceiling", None)
    if ceiling is not None:
        checker = getattr(ctx, "bot_access_check", None)
        try:
            if not callable(checker) or checker() is not True:
                return AccessDecision(False, "bot_access_revoked")
        except Exception:
            return AccessDecision(False, "bot_access_revoked")
        if tool_name == "terminal" and not ceiling.grants.cli_commands:
            return AccessDecision(False, "bot_cli_not_selected")
        if tool_name == "skill_view":
            name = str(args.get("name") or "")
            if "*" not in ceiling.grants.skills_load and name not in ceiling.grants.skills_load:
                return AccessDecision(False, "bot_skill_not_selected", detail=name)
        bounded = decide_tool_argument_access(ceiling, tool_name, args)
        if not bounded.allowed:
            return bounded
    decision = decide_tool_argument_access(ctx.access if ctx is not None else None, tool_name, args)
    root = getattr(ctx, "project_workspace", "") if ctx is not None else ""
    if not root or tool_name not in {"read_file", "search_files", "write_file", "patch"}:
        return decision
    raw = str(args.get("path") or ".")
    # Grant only an explicit absolute file path. Relative paths retain the
    # normal engine policy because a tool backend may resolve its cwd differently.
    path = Path(os.path.expanduser(raw))
    workspace = Path(root)
    if not path.is_absolute() or not workspace.is_absolute():
        return decision
    try:
        lexical = Path(os.path.abspath(path))
        lexical.relative_to(workspace)
    except (ValueError, OSError):
        return decision
    denied = AccessDecision(False, "project_file_access_denied", detail=raw)
    try:
        # No project scope follows symlinks, including symlinked ancestors.
        if ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
            return denied
        path.resolve().relative_to(workspace.resolve())
        check = getattr(ctx, "project_access_check", None)
        if not callable(check) or check(str(path), tool_name in {"write_file", "patch"}) is not True:
            return denied
    except Exception:
        return denied
    # Never override denied globs, tool permissions, or other argument denials.
    if decision.reason in {"file_read_root_not_allowed", "file_write_root_not_allowed"}:
        return AccessDecision(True, "project_file_scope_allowed")
    return decision
