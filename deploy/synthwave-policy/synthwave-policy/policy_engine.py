"""Synthwave autonomy policy engine: AUTO / ASK / BLOCK decisions for agent tool calls.

Pure stdlib plus PyYAML. No Hermes imports at module level, so the same module serves the
Hermes plugin hook (``__init__.py``), the ``bin/policy_check.py`` explainer and the tests.

Policy file schema (see ``policy/autonomy.example.yaml`` for the annotated version)::

    version: 1
    mode: observe | enforce          # default observe
    defaults: {decision: AUTO}       # decision when no rule matches
    log: {max_bytes: 5242880, keep: 1}
    rules:
      - id: unique-id
        decision: AUTO | ASK | BLOCK
        description: human readable sentence (shown to the agent and in the WebUI)
        tool: [glob, ...]            # tool name globs, case-insensitive
        mcp: {server: regex, tool: regex}
        command: [regex, ...]        # per executed shell command (terminal tool)
        command_text: [regex, ...]   # anywhere in the raw command string
        writes_path: [glob, ...]     # file targets written (write tools, shell redirects, tee, cp, ...)
        rm_recursive_outside: [glob, ...]   # recursive rm with a target outside these globs
        args: {name: regex}          # argument value regex (re.search on str(value))
        args_text: [regex, ...]      # regex on the JSON dump of all arguments
        unless: {...same matchers...}
    profiles:
      <profile>:
        mode: enforce
        defaults: {decision: AUTO}
        overrides: {rule-id: AUTO | ASK | BLOCK}
        disable: [rule-id]
        rules: [...]                 # extra rules for this profile only

Matching: within one rule every matcher key present must match (AND); inside a list any entry
may match (OR). When several rules match, the most severe decision wins (BLOCK > ASK > AUTO);
ties go to the first rule (profile rules first, then global rules in file order).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DECISIONS = ("AUTO", "ASK", "BLOCK")
SEVERITY = {"AUTO": 0, "ASK": 1, "BLOCK": 2}
MODES = ("observe", "enforce")
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 1

TERMINAL_TOOLS = ("terminal",)
WRITE_TOOL_PATH_KEYS = ("path", "file_path", "filepath", "target_path")

MATCHER_KEYS = {
    "tool", "mcp", "command", "command_text", "writes_path", "rm_recursive_outside", "args", "args_text",
    "code_text", "undecidable", "touches_path", "page_url",
}
# Longest command / code / argument text the engine evaluates. Anything longer is ASK
# ("command too long to evaluate"): regexes over megabytes of attacker-shaped text are a DoS risk.
MAX_EVAL_CHARS = 64 * 1024
RULE_KEYS = MATCHER_KEYS | {"id", "decision", "description", "unless", "tags"}
TOP_KEYS = {"version", "mode", "defaults", "log", "rules", "profiles"}
PROFILE_KEYS = {"mode", "defaults", "overrides", "disable", "rules"}


class PolicyError(ValueError):
    """The policy file is unreadable or invalid."""


# ---------------------------------------------------------------------------------------------
# Shell parsing
# ---------------------------------------------------------------------------------------------

_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
_WRAPPERS_NO_ARG = {"nohup", "time", "exec", "command", "builtin", "stdbuf", "unbuffer", "caffeinate"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT_TOKEN = re.compile(r"^(\d*|&)(>>?|>\|)(.*)$")
_SSH_OPTS_WITH_VALUE = set("bcDEeFIiJLlmOopQRSWw")


@dataclass
class Segment:
    """One simple command as the shell would execute it.

    ``code`` holds program text handed to an interpreter (``python -c``, ``node -e``, a heredoc or
    here-string fed to python/perl/..., text echoed into ``| python``). ``undecidable`` is set when
    what runs cannot be known from the text: a variable or substitution in command position that
    could not be resolved, or a shell/interpreter reading its program from a pipe whose producer is
    not a plain echo/printf.
    """

    raw: str
    argv: List[str]
    write_targets: List[str]
    heredocs: List[str] = field(default_factory=list)
    touch_targets: List[str] = field(default_factory=list)
    code: List[str] = field(default_factory=list)
    herestrings: List[str] = field(default_factory=list)
    dynamic: bool = False
    undecidable: bool = False

    @property
    def normalized(self) -> str:
        return " ".join(self.argv)


def _read_balanced(text: str, i: int) -> Tuple[str, int]:
    """Read from text[i] (just after an opening paren) to the matching ``)``. Returns (inner, end_index)."""
    depth = 1
    start = i
    quote = ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "\\":
            i += 2
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return text[start:], len(text)


_LOOKUP_SUBST = re.compile(r"^\s*(?:command\s+-[vV]|which|type\s+-[pP]|whereis|readlink\s+-f\s+\$\(\s*which)\s+"
                           r"([A-Za-z0-9_./~+-]{1,200})\s*\)?\s*$")


def _lookup_name(inner: str) -> Optional[str]:
    """``command -v waapi`` / ``which waapi`` -> ``waapi`` (the substitution only names a program)."""
    m = _LOOKUP_SUBST.match(inner or "")
    return os.path.basename(m.group(1)) if m else None


_HEREDOC_START = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z0-9_.\-]+)\2")


def split_shell(text: str, _depth: int = 0) -> List[Tuple[str, List[str], bool]]:
    """Split a shell string into (segment_text, heredoc_bodies, piped) tuples.

    Breaks on unquoted ``; & | newline ( )``; command substitutions ``$( )`` and backticks become
    their own segments (they execute); heredoc bodies are attached to the segment that opened them
    and are NOT treated as commands here. Comments are dropped. ``piped`` is True when the segment's
    output feeds the next segment through a single ``|`` (or ``|&``).
    A substitution that only looks up a program (``$(command -v X)``, ``$(which X)``) is replaced by
    the program name, so ``"$(command -v waapi)" send`` is seen as ``waapi send``.
    """
    if _depth > 6:
        return []
    out: List[Tuple[str, List[str], bool]] = []
    extra: List[Tuple[str, List[str], bool]] = []
    cur: List[str] = []
    cur_heredocs: List[str] = []
    pending: List[Tuple[str, bool]] = []  # (delimiter, strip_tabs)
    quote = ""
    i = 0
    n = len(text)

    def flush(piped: bool = False) -> None:
        seg = "".join(cur).strip()
        if seg or cur_heredocs:
            out.append((seg, list(cur_heredocs), piped))
        cur.clear()
        cur_heredocs.clear()

    while i < n:
        ch = text[i]
        if quote == "'":
            cur.append(ch)
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n:
                cur.append(text[i:i + 2])
                i += 2
                continue
            if ch == '"':
                quote = ""
                cur.append(ch)
                i += 1
                continue
            if text.startswith("$(", i) and not text.startswith("$((", i):
                inner, j = _read_balanced(text, i + 2)
                extra.extend(split_shell(inner, _depth + 1))
                cur.append(text[i:j])
                i = j
                continue
            if ch == "`":
                j = text.find("`", i + 1)
                j = n if j < 0 else j
                extra.extend(split_shell(text[i + 1:j], _depth + 1))
                cur.append(text[i:j + 1])
                i = j + 1
                continue
            cur.append(ch)
            i += 1
            continue
        # unquoted
        if ch == "\\" and i + 1 < n:
            if text[i + 1] == "\n":  # line continuation
                i += 2
                continue
            cur.append(text[i:i + 2])
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            i += 1
            continue
        if ch == "#" and (not cur or cur[-1][-1:].isspace()):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("$((", i):
            inner, j = _read_balanced(text, i + 3)
            cur.append(text[i:j])
            i = j
            continue
        if text.startswith("$(", i):
            inner, j = _read_balanced(text, i + 2)
            extra.extend(split_shell(inner, _depth + 1))
            cur.append(_lookup_name(inner) or "__SUBST__")
            i = j
            continue
        if text.startswith("<(", i) or text.startswith(">(", i):
            inner, j = _read_balanced(text, i + 2)
            extra.extend(split_shell(inner, _depth + 1))
            cur.append("__SUBST__")
            i = j
            continue
        if ch == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            extra.extend(split_shell(text[i + 1:j], _depth + 1))
            cur.append(_lookup_name(text[i + 1:j]) or "__SUBST__")
            i = j + 1
            continue
        if text.startswith("<<<", i):
            cur.append("<<<")
            i += 3
            continue
        if text.startswith("<<", i):
            m = _HEREDOC_START.match(text, i)
            if m:
                pending.append((m.group(3), m.group(1) == "-"))
                cur.append(m.group(0))
                i = m.end()
                continue
        if ch == "\n":
            if pending:
                # Consume heredoc bodies that start on the next line.
                seg_heredocs: List[str] = []
                pos = i + 1
                for delim, strip_tabs in pending:
                    body_lines: List[str] = []
                    while pos <= n:
                        j = text.find("\n", pos)
                        line = text[pos:] if j < 0 else text[pos:j]
                        pos = n + 1 if j < 0 else j + 1
                        check = line.lstrip("\t") if strip_tabs else line
                        if check.strip() == delim:
                            break
                        body_lines.append(line)
                    seg_heredocs.append("\n".join(body_lines))
                pending.clear()
                cur_heredocs.extend(seg_heredocs)
                flush()
                i = pos
                continue
            flush()
            i += 1
            continue
        if ch == "|":
            if text.startswith("||", i):
                flush()
                i += 2
                continue
            flush(piped=True)
            i += 2 if text.startswith("|&", i) else 1
            continue
        if ch in ";&()":
            flush()
            i += 1
            continue
        cur.append(ch)
        i += 1
    flush()
    return out + extra


def _strip_wrappers(argv: List[str]) -> List[str]:
    """Drop env assignments and transparent wrappers (sudo, env, timeout, nice, xargs, command, ...).

    ``env -S 'cmd args'`` is split into words; ``command -v X`` (a lookup, not a run) is kept as is.
    """
    argv = list(argv)
    i = 0
    guard = 0
    while i < len(argv) and guard < 40:
        guard += 1
        tok = argv[i]
        base = os.path.basename(tok)
        if _ENV_ASSIGN.match(tok):
            i += 1
            continue
        if base == "command":
            j = i + 1
            while j < len(argv) and argv[j].startswith("-") and argv[j] != "--":
                if "v" in argv[j] or "V" in argv[j]:
                    return argv[i:i + 1] + argv[i + 1:]  # lookup only
                j += 1
            if j < len(argv) and argv[j] == "--":
                j += 1
            i = j
            continue
        if base in _WRAPPERS_NO_ARG:
            i += 1
            while i < len(argv) and argv[i].startswith("-") and argv[i] != "-":
                if base == "exec" and argv[i] == "-a":
                    i += 1
                i += 1
            continue
        if base in ("sudo", "doas"):
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                if argv[i] in ("-u", "-g", "-C", "-D", "-h", "-p", "-U", "-r", "-t"):
                    i += 1
                i += 1
            continue
        if base == "env":
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or _ENV_ASSIGN.match(argv[i])):
                a = argv[i]
                split_val = None
                if a in ("-S", "--split-string") and i + 1 < len(argv):
                    split_val = argv[i + 1]
                    argv = argv[:i] + _tokenize(split_val) + argv[i + 2:]
                    break
                if a.startswith("--split-string="):
                    split_val = a.split("=", 1)[1]
                elif a.startswith("-S") and len(a) > 2:
                    split_val = a[2:]
                if split_val is not None:
                    argv = argv[:i] + _tokenize(split_val) + argv[i + 1:]
                    break
                if a in ("-u", "-C"):
                    i += 1
                i += 1
            continue
        if base in ("timeout", "nice", "ionice", "chrt", "taskset"):
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                if argv[i] in ("-n", "-s", "-k", "-c", "-p"):
                    i += 1
                i += 1
            if base in ("timeout", "chrt", "taskset") and i < len(argv):
                i += 1  # duration / priority / mask
            continue
        if base in ("xargs", "watch", "flock", "parallel"):
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                if argv[i] in ("-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "-w", "-j"):
                    i += 1
                i += 1
            if base == "flock" and i < len(argv):
                i += 1  # lock file
            continue
        break
    rest = argv[i:]
    if rest:
        rest = [os.path.basename(rest[0])] + rest[1:]
    return rest


def _tokenize(seg: str) -> List[str]:
    try:
        return shlex.split(seg, comments=False, posix=True)
    except ValueError:
        return seg.split()


def _extract_redirects(tokens: List[str], herestrings: Optional[List[str]] = None) -> Tuple[List[str], List[str]]:
    argv: List[str] = []
    targets: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        m = _REDIRECT_TOKEN.match(tok)
        if m and not tok.startswith(">(") and (m.group(1) or tok.startswith(">")):
            dest = m.group(3)
            if not dest and i + 1 < len(tokens):
                dest = tokens[i + 1]
                i += 1
            if dest and not dest.startswith("&") and not dest.startswith("/dev/"):
                targets.append(dest)
            i += 1
            continue
        if tok in ("<", "<<<") or (tok.startswith("<<") and len(tok) > 2) or tok == "<<":
            # input redirect / heredoc marker: drop it and its operand (a here-string is kept aside)
            if tok in ("<", "<<<", "<<") and i + 1 < len(tokens):
                if tok == "<<<" and herestrings is not None:
                    herestrings.append(tokens[i + 1])
                i += 1
            i += 1
            continue
        if tok.startswith("<") and len(tok) > 1:
            i += 1
            continue
        argv.append(tok)
        i += 1
    return argv, targets


def _command_write_targets(argv: List[str]) -> List[str]:
    """Files a command writes to, for the commands we understand. Conservative: unknown -> []."""
    if not argv:
        return []
    cmd = argv[0]
    args = argv[1:]
    plain = [a for a in args if not a.startswith("-")]
    if cmd == "tee":
        return plain
    if cmd in ("cp", "mv", "install", "rsync", "scp", "ln") and len(plain) >= 2:
        if "-t" in args:
            idx = args.index("-t")
            return [args[idx + 1]] if idx + 1 < len(args) else []
        return [plain[-1]]
    if cmd in ("sed", "perl") and any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in args):
        # sed -i 'script' files...  /  sed -i -e script files...
        files: List[str] = []
        skip_next = False
        script_seen = any(a in ("-e", "--expression") for a in args)
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in ("-e", "--expression", "-f"):
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            if not script_seen:
                script_seen = True
                continue
            files.append(a)
        return files
    if cmd == "truncate":
        return [a for a in plain]
    if cmd == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if cmd == "touch":
        return plain
    return []


def _command_touch_targets(argv: List[str]) -> List[str]:
    """Files a command deletes, moves away or changes the mode of (on top of what it writes)."""
    if not argv:
        return []
    cmd = argv[0]
    plain = [a for a in argv[1:] if not a.startswith("-")]
    if cmd in ("rm", "unlink", "shred", "rmdir", "chattr", "srm"):
        return plain
    if cmd in ("mv",):
        return plain  # sources disappear, the destination is written
    if cmd in ("chmod", "chown", "chgrp", "setfacl") and len(plain) >= 2:
        return plain[1:]
    if cmd == "find" and any(a in ("-delete",) for a in argv):
        return plain[:1]
    return []


_INTERPRETER = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|node(js)?|deno|bun|perl[0-9.]*|ruby[0-9.]*|php[0-9.]*|lua[0-9.]*|"
                          r"luajit|osascript|pwsh|powershell|Rscript|tclsh|[gmn]?awk)$")
_CODE_FLAGS = {"-c", "-e", "-E", "-r", "-p", "--eval", "--print", "-Command", "-command", "-x"}
_STDIN_PRODUCERS = ("echo", "printf")
_SEND_VERB = re.compile(r"^\+?(send|send-media|send-file|reply|reply-all|forward|card|comment|react|schedule|publish|post)$",
                        re.IGNORECASE)


def _producer_text(seg: Optional[Segment]) -> Optional[str]:
    """What ``echo ... |`` / ``printf ... |`` writes to the pipe, or None when it cannot be known."""
    if seg is None or not seg.argv or seg.argv[0] not in _STDIN_PRODUCERS:
        return None
    words = [a for a in seg.argv[1:] if not (seg.argv[0] == "echo" and a in ("-e", "-n", "-E", "-ne", "-en"))]
    return " ".join(words).replace("\\n", "\n").replace("\\t", "\t")


def _interpreter_payloads(argv: List[str]) -> Tuple[List[str], bool]:
    """Program text given on the command line, and whether the program comes from stdin."""
    cmd = argv[0]
    args = argv[1:]
    code: List[str] = []
    if re.match(r"^[gmn]?awk$", cmd):
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-f", "--file"):
                return code, False
            if a in ("-v", "-F", "--assign", "--field-separator"):
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            return [a], False
        return code, False
    if cmd in ("deno", "bun") and args[:1] == ["eval"]:
        return [" ".join(args[1:])], False
    i = 0
    script = False
    module = False
    while i < len(args):
        a = args[i]
        if a in _CODE_FLAGS and i + 1 < len(args):
            code.append(args[i + 1])
            i += 2
            continue
        if a.startswith(("-c", "-e")) and len(a) > 2 and not a.startswith("--") and cmd.startswith(("python", "perl", "ruby", "node")):
            code.append(a[2:])
            i += 1
            continue
        if a in ("-m",) and i + 1 < len(args):
            module = True
            break
        if a == "-":
            break
        if a.startswith("-"):
            i += 1
            continue
        script = True
        break
    reads_stdin = not code and not script and not module
    return code, reads_stdin


def _ssh_remote_command(argv: List[str]) -> Optional[str]:
    i = 1
    while i < len(argv):
        a = argv[i]
        if a.startswith("-") and len(a) >= 2:
            if a[-1] in _SSH_OPTS_WITH_VALUE and len(a) == 2:
                i += 2
                continue
            i += 1
            continue
        break
    # argv[i] is the host
    if i + 1 < len(argv):
        return " ".join(argv[i + 1:])
    return None


def _find_exec_commands(argv: List[str]) -> List[str]:
    """``find ... -exec cmd args ;`` / ``-execdir`` / ``-ok``: the commands find runs."""
    out: List[str] = []
    i = 1
    while i < len(argv):
        if argv[i] in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            words: List[str] = []
            while j < len(argv) and argv[j] not in (";", "\\;", "+"):
                words.append(argv[j])
                j += 1
            if words:
                out.append(shlex.join(words))
            i = j + 1
            continue
        i += 1
    return out


_VAR_REF = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))$")


def parse_command(command: str, _depth: int = 0, _vars: Optional[Dict[str, str]] = None) -> List[Segment]:
    """Parse a shell command string into executed simple commands (recursing into ``bash -c``,
    ``eval``, ``ssh host cmd``, ``env -S``, ``find -exec``, heredocs, here-strings and echo/printf
    text piped into a shell). Simple ``VAR=value`` assignments made earlier in the same command are
    substituted when the variable is used in command position."""
    segments: List[Segment] = []
    if not isinstance(command, str) or not command.strip() or _depth > 4:
        return segments
    variables: Dict[str, str] = _vars if _vars is not None else {}
    prev: Optional[Segment] = None
    prev_piped = False
    for raw, heredocs, piped in split_shell(command):
        herestrings: List[str] = []
        tokens = _tokenize(raw)
        tokens, targets = _extract_redirects(tokens, herestrings)
        lead = tokens[1:] if tokens[:1] in (["export"], ["local"], ["declare"], ["readonly"]) else tokens
        if lead and all(_ENV_ASSIGN.match(t) for t in lead):
            for t in lead:
                k, _, v = t.partition("=")
                variables[k] = v
        argv = _strip_wrappers(tokens)
        dynamic = False
        if argv:
            m = _VAR_REF.match(argv[0])
            if m:
                val = variables.get(m.group(1) or m.group(2))
                if val:
                    argv = _strip_wrappers(_tokenize(val) + argv[1:])
                else:
                    dynamic = True
            elif argv[0].startswith(("$(", "`")):
                inner = argv[0][2:-1] if argv[0].startswith("$(") else argv[0][1:-1]
                name = _lookup_name(inner)
                if name:
                    argv = [name] + argv[1:]
                else:
                    dynamic = True
            elif argv[0] == "__SUBST__" or "$" in argv[0]:
                dynamic = True
        if not dynamic:
            argv = [a for a in argv if a != "__SUBST__"]
        targets = targets + _command_write_targets(argv)
        seg = Segment(raw=raw, argv=argv, write_targets=targets, heredocs=heredocs,
                      touch_targets=targets + _command_touch_targets(argv), herestrings=herestrings,
                      dynamic=dynamic)
        if dynamic and any(_SEND_VERB.match(a) for a in argv[1:12]):
            seg.undecidable = True
        segments.append(seg)
        producer = prev if prev_piped else None
        prev, prev_piped = seg, piped
        if not argv:
            continue
        cmd = argv[0]
        if cmd in _SHELLS:
            stdin_mode = "-s" in argv[1:]
            if "-c" in argv[1:]:
                idx = argv.index("-c")
                if idx + 1 < len(argv):
                    segments.extend(parse_command(argv[idx + 1], _depth + 1, variables))
            else:
                file_args = [a for a in argv[1:] if not a.startswith("-")]
                if stdin_mode or not file_args or file_args == ["-"]:
                    bodies = heredocs + herestrings
                    text = _producer_text(producer)
                    if text is not None:
                        bodies.append(text)
                    elif producer is not None and not bodies:
                        seg.undecidable = True
                    for body in bodies:
                        segments.extend(parse_command(body, _depth + 1, variables))
        elif _INTERPRETER.match(cmd):
            code, reads_stdin = _interpreter_payloads(argv)
            if reads_stdin:
                code.extend(heredocs + herestrings)
                text = _producer_text(producer)
                if text is not None:
                    code.append(text)
                elif producer is not None and not heredocs and not herestrings:
                    seg.undecidable = True
            seg.code = code
        elif cmd == "eval":
            segments.extend(parse_command(" ".join(argv[1:]), _depth + 1, variables))
        elif cmd in ("source", "."):
            pass  # the sourced file is not visible here (documented gap)
        elif cmd == "find":
            for inner in _find_exec_commands(argv):
                segments.extend(parse_command(inner, _depth + 1, variables))
        elif cmd == "ssh":
            remote = _ssh_remote_command(argv)
            if remote:
                segments.extend(parse_command(remote, _depth + 1, variables))
            else:
                for body in heredocs:
                    segments.extend(parse_command(body, _depth + 1, variables))
    return segments


# ---------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------

def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _compile_regex(pattern: Any, where: str) -> "re.Pattern[str]":
    if not isinstance(pattern, str) or not pattern:
        raise PolicyError(f"{where}: regex must be a non-empty string")
    try:
        return re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error as exc:
        raise PolicyError(f"{where}: invalid regex {pattern!r}: {exc}") from exc


def _check_globs(values: Any, where: str) -> List[str]:
    out = []
    for v in _as_list(values):
        if not isinstance(v, str) or not v:
            raise PolicyError(f"{where}: glob must be a non-empty string")
        out.append(v)
    return out


@dataclass
class Matchers:
    tool: List[str] = field(default_factory=list)
    mcp_server: Optional["re.Pattern[str]"] = None
    mcp_tool: Optional["re.Pattern[str]"] = None
    has_mcp: bool = False
    command: List["re.Pattern[str]"] = field(default_factory=list)
    command_text: List["re.Pattern[str]"] = field(default_factory=list)
    writes_path: List[str] = field(default_factory=list)
    rm_allow: Optional[List[str]] = None
    args: Dict[str, "re.Pattern[str]"] = field(default_factory=dict)
    args_text: List["re.Pattern[str]"] = field(default_factory=list)
    code_text: List["re.Pattern[str]"] = field(default_factory=list)
    undecidable: Optional[bool] = None
    touches_path: List[str] = field(default_factory=list)
    page_url: List["re.Pattern[str]"] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.tool or self.has_mcp or self.command or self.command_text or self.writes_path
                    or self.rm_allow is not None or self.args or self.args_text or self.code_text
                    or self.undecidable is not None or self.touches_path or self.page_url)

    def uses_shell(self) -> bool:
        return bool(self.command or self.command_text or self.rm_allow is not None or self.undecidable is not None)


def _compile_matchers(spec: Dict[str, Any], where: str) -> Matchers:
    m = Matchers()
    m.tool = [t.lower() for t in _check_globs(spec.get("tool"), f"{where}.tool")]
    if "mcp" in spec:
        mcp = spec["mcp"]
        if not isinstance(mcp, dict) or not (set(mcp) <= {"server", "tool"}) or not mcp:
            raise PolicyError(f"{where}.mcp: expected a mapping with 'server' and/or 'tool'")
        m.has_mcp = True
        if "server" in mcp:
            m.mcp_server = _compile_regex(mcp["server"], f"{where}.mcp.server")
        if "tool" in mcp:
            m.mcp_tool = _compile_regex(mcp["tool"], f"{where}.mcp.tool")
    m.command = [_compile_regex(p, f"{where}.command") for p in _as_list(spec.get("command"))]
    m.command_text = [_compile_regex(p, f"{where}.command_text") for p in _as_list(spec.get("command_text"))]
    m.writes_path = _check_globs(spec.get("writes_path"), f"{where}.writes_path")
    if "rm_recursive_outside" in spec:
        m.rm_allow = _check_globs(spec.get("rm_recursive_outside"), f"{where}.rm_recursive_outside")
    args = spec.get("args") or {}
    if not isinstance(args, dict):
        raise PolicyError(f"{where}.args: expected a mapping of argument name to regex")
    m.args = {str(k): _compile_regex(v, f"{where}.args.{k}") for k, v in args.items()}
    m.args_text = [_compile_regex(p, f"{where}.args_text") for p in _as_list(spec.get("args_text"))]
    m.code_text = [_compile_regex(p, f"{where}.code_text") for p in _as_list(spec.get("code_text"))]
    if "undecidable" in spec:
        if not isinstance(spec["undecidable"], bool):
            raise PolicyError(f"{where}.undecidable: expected true or false")
        m.undecidable = spec["undecidable"]
    m.touches_path = _check_globs(spec.get("touches_path"), f"{where}.touches_path")
    m.page_url = [_compile_regex(p, f"{where}.page_url") for p in _as_list(spec.get("page_url"))]
    return m


@dataclass
class Rule:
    id: str
    decision: str
    description: str
    when: Matchers
    unless: Optional[Matchers]
    tags: List[str]
    origin: str  # "global" or "profile:<name>"

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "decision": self.decision, "description": self.description,
                "tags": list(self.tags), "origin": self.origin}


def _compile_rule(spec: Any, where: str, origin: str) -> Rule:
    if not isinstance(spec, dict):
        raise PolicyError(f"{where}: rule must be a mapping")
    unknown = set(spec) - RULE_KEYS
    if unknown:
        raise PolicyError(f"{where}: unknown key(s) {sorted(unknown)}")
    rid = spec.get("id")
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", rid or ""):
        raise PolicyError(f"{where}: 'id' must be a slug (letters, digits, . _ : -)")
    decision = str(spec.get("decision", "")).upper()
    if decision not in DECISIONS:
        raise PolicyError(f"{where} ({rid}): decision must be one of {DECISIONS}")
    when = _compile_matchers(spec, f"{where} ({rid})")
    if when.is_empty():
        raise PolicyError(f"{where} ({rid}): rule has no matcher")
    unless = None
    if "unless" in spec:
        u = spec["unless"]
        if not isinstance(u, dict) or not (set(u) <= MATCHER_KEYS):
            raise PolicyError(f"{where} ({rid}).unless: expected a mapping of matchers")
        unless = _compile_matchers(u, f"{where} ({rid}).unless")
    desc = spec.get("description") or rid
    return Rule(id=rid, decision=decision, description=str(desc).strip(), when=when, unless=unless,
                tags=[str(t) for t in _as_list(spec.get("tags"))], origin=origin)


@dataclass
class ProfilePolicy:
    mode: Optional[str] = None
    default_decision: Optional[str] = None
    overrides: Dict[str, str] = field(default_factory=dict)
    disable: List[str] = field(default_factory=list)
    rules: List[Rule] = field(default_factory=list)


@dataclass
class Policy:
    mode: str
    default_decision: str
    rules: List[Rule]
    profiles: Dict[str, ProfilePolicy]
    max_bytes: int
    keep: int
    source: str = ""

    def mode_for(self, profile: str) -> str:
        p = self.profiles.get(profile)
        return (p.mode if p and p.mode else self.mode)

    def rules_for(self, profile: str) -> List[Tuple[Rule, str]]:
        """Effective (rule, decision) list for a profile, profile rules first."""
        p = self.profiles.get(profile) or ProfilePolicy()
        out: List[Tuple[Rule, str]] = []
        for r in p.rules + self.rules:
            if r.id in p.disable:
                continue
            out.append((r, p.overrides.get(r.id, r.decision)))
        return out

    def default_for(self, profile: str) -> str:
        p = self.profiles.get(profile)
        return (p.default_decision if p and p.default_decision else self.default_decision)


def _parse_decision(value: Any, where: str) -> str:
    d = str(value or "").upper()
    if d not in DECISIONS:
        raise PolicyError(f"{where}: decision must be one of {DECISIONS}")
    return d


def compile_policy(data: Any, source: str = "") -> Policy:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PolicyError("policy root must be a mapping")
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise PolicyError(f"unknown top-level key(s) {sorted(unknown)}")
    mode = str(data.get("mode", "observe")).lower()
    if mode not in MODES:
        raise PolicyError(f"mode must be one of {MODES}")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise PolicyError("defaults must be a mapping")
    default_decision = _parse_decision(defaults.get("decision", "AUTO"), "defaults.decision")
    log = data.get("log") or {}
    if not isinstance(log, dict):
        raise PolicyError("log must be a mapping")
    try:
        max_bytes = int(log.get("max_bytes", DEFAULT_MAX_BYTES))
        keep = int(log.get("keep", DEFAULT_KEEP))
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"log: {exc}") from exc
    rules_raw = data.get("rules") or []
    if not isinstance(rules_raw, list):
        raise PolicyError("rules must be a list")
    rules = [_compile_rule(r, f"rules[{i}]", "global") for i, r in enumerate(rules_raw)]
    seen: set = set()
    for r in rules:
        if r.id in seen:
            raise PolicyError(f"duplicate rule id {r.id!r}")
        seen.add(r.id)
    profiles: Dict[str, ProfilePolicy] = {}
    profiles_raw = data.get("profiles") or {}
    if not isinstance(profiles_raw, dict):
        raise PolicyError("profiles must be a mapping of profile name to settings")
    for name, praw in profiles_raw.items():
        where = f"profiles.{name}"
        praw = praw or {}
        if not isinstance(praw, dict):
            raise PolicyError(f"{where}: expected a mapping")
        unknown = set(praw) - PROFILE_KEYS
        if unknown:
            raise PolicyError(f"{where}: unknown key(s) {sorted(unknown)}")
        pp = ProfilePolicy()
        if "mode" in praw:
            pm = str(praw["mode"]).lower()
            if pm not in MODES:
                raise PolicyError(f"{where}.mode must be one of {MODES}")
            pp.mode = pm
        pdef = praw.get("defaults") or {}
        if not isinstance(pdef, dict):
            raise PolicyError(f"{where}.defaults must be a mapping")
        if "decision" in pdef:
            pp.default_decision = _parse_decision(pdef["decision"], f"{where}.defaults.decision")
        ov = praw.get("overrides") or {}
        if not isinstance(ov, dict):
            raise PolicyError(f"{where}.overrides must be a mapping of rule id to decision")
        prules = [_compile_rule(r, f"{where}.rules[{i}]", f"profile:{name}")
                  for i, r in enumerate(praw.get("rules") or [])]
        known = seen | {r.id for r in prules}
        for rid, dec in ov.items():
            if rid not in known:
                raise PolicyError(f"{where}.overrides: unknown rule id {rid!r}")
            pp.overrides[str(rid)] = _parse_decision(dec, f"{where}.overrides.{rid}")
        for rid in _as_list(praw.get("disable")):
            if rid not in known:
                raise PolicyError(f"{where}.disable: unknown rule id {rid!r}")
            pp.disable.append(str(rid))
        pp.rules = prules
        profiles[str(name)] = pp
    return Policy(mode=mode, default_decision=default_decision, rules=rules, profiles=profiles,
                  max_bytes=max(1024, max_bytes), keep=max(0, keep), source=source)


def load_policy_file(path: Path) -> Policy:
    try:
        import yaml  # PyYAML ships with Hermes
    except ImportError as exc:  # pragma: no cover
        raise PolicyError(f"PyYAML is not available: {exc}") from exc
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"YAML syntax error: {str(exc).splitlines()[0]}") from exc
    return compile_policy(data, source=str(path))


_ENFORCE_HINT = re.compile(r"^mode:\s*['\"]?enforce\b", re.MULTILINE)


def raw_mode_hint(path: Path) -> Optional[str]:
    """Best-effort mode for a file that does not parse: a top-level ``mode: enforce`` line."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return "enforce" if _ENFORCE_HINT.search(text) else None


# ---------------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------------

WRITE_TOOLS = ("write_file", "patch", "edit", "edit_file", "str_replace", "create_file", "append_file", "multi_edit")
CODE_TOOLS = ("execute_code",)
_V4A_PATH = re.compile(r"(?m)^\*\*\* (?:Update File|Add File|Delete File|Move to):[ \t]*([^\n]{1,1024})$")


@dataclass
class CallContext:
    tool_name: str
    args: Dict[str, Any]
    page_url: str = ""
    _segments: Optional[List[Segment]] = None
    _args_text: Optional[str] = None
    _code_texts: Optional[List[str]] = None

    @property
    def tool_lower(self) -> str:
        return (self.tool_name or "").lower()

    @property
    def command(self) -> str:
        if self.tool_lower in TERMINAL_TOOLS:
            c = self.args.get("command")
            return c if isinstance(c, str) else ""
        return ""

    def primary_text_len(self) -> int:
        """Length of the text whose structure decides the call: the shell command or the code."""
        if self.tool_lower in TERMINAL_TOOLS:
            return len(self.command)
        if self.tool_lower in CODE_TOOLS:
            return sum(len(x) for x in _iter_strings(self.args))
        return 0

    @property
    def segments(self) -> List[Segment]:
        if self._segments is None:
            self._segments = parse_command(self.command) if self.command else []
        return self._segments

    @property
    def args_text(self) -> str:
        if self._args_text is None:
            try:
                dumped = json.dumps(self.args, ensure_ascii=False, sort_keys=True, default=str)
            except Exception:
                dumped = str(self.args)
            # JSON escapes newlines, so also append the raw string values for multi-line text.
            text = dumped + "\n" + "\n".join(_iter_strings(self.args))
            # Bounded scan: a huge file body is scanned in its first MAX_EVAL_CHARS * 2 characters.
            self._args_text = text[:MAX_EVAL_CHARS * 2]
        return self._args_text

    @property
    def code_texts(self) -> List[str]:
        """Program text the call executes: interpreter payloads in a shell command, or the code of
        execute_code. Plain shell words (grep patterns, echo text) are not code."""
        if self._code_texts is None:
            if self.tool_lower in TERMINAL_TOOLS:
                self._code_texts = [c for s in self.segments for c in s.code if isinstance(c, str)]
            elif self.tool_lower in CODE_TOOLS:
                self._code_texts = _iter_strings(self.args)
            else:
                self._code_texts = []
        return self._code_texts

    @property
    def undecidable(self) -> bool:
        return any(s.undecidable for s in self.segments)

    def mcp_parts(self) -> Optional[Tuple[str, str]]:
        name = self.tool_name or ""
        if name.startswith("mcp__") or name.startswith("connectors__"):
            rest = name.split("__", 1)[1]
            if "__" in rest:
                server, tool = rest.split("__", 1)
                return server, tool
            return "", rest
        if name.startswith("mcp_"):
            return "", name[4:]  # legacy single-underscore form: server boundary unknown
        return None

    def write_targets(self) -> List[str]:
        if self.tool_lower in TERMINAL_TOOLS:
            out: List[str] = []
            for s in self.segments:
                out.extend(s.write_targets)
            return out
        if self.tool_lower in WRITE_TOOLS:
            out = []
            for k in WRITE_TOOL_PATH_KEYS:
                v = self.args.get(k)
                if isinstance(v, str) and v:
                    out.append(v)
            patch_text = self.args.get("patch")
            if isinstance(patch_text, str):
                out.extend(m.strip() for m in _V4A_PATH.findall(patch_text[:MAX_EVAL_CHARS * 4]))
            return out
        return []

    def touch_targets(self) -> List[str]:
        if self.tool_lower in TERMINAL_TOOLS:
            out: List[str] = []
            for s in self.segments:
                out.extend(s.touch_targets)
            return out
        return self.write_targets()


def _iter_strings(value: Any, depth: int = 0) -> List[str]:
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _iter_strings(v, depth + 1)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _iter_strings(v, depth + 1)]
    return []


def _glob_match(path: str, patterns: Iterable[str], basename: bool = True) -> bool:
    if not path:
        return False
    expanded = os.path.expanduser(path)
    if expanded.startswith("/"):
        expanded = os.path.normpath(expanded)  # /tmp/../home must not pass as /tmp/*
        path = expanded
    candidates = {path, expanded}
    if basename:
        candidates.add(os.path.basename(path.rstrip("/")) or path)
    if not path.startswith(("/", "~", "$")):
        candidates.add("./" + path)
    for pat in patterns:
        pat_x = os.path.expanduser(pat)
        for c in candidates:
            if fnmatch.fnmatchcase(c, pat) or fnmatch.fnmatchcase(c, pat_x):
                return True
    return False


def _rm_recursive_targets(seg: Segment) -> Optional[List[str]]:
    argv = seg.argv
    if not argv or argv[0] != "rm":
        return None
    recursive = False
    targets: List[str] = []
    end_opts = False
    for a in argv[1:]:
        if not end_opts and a == "--":
            end_opts = True
            continue
        if not end_opts and a.startswith("--"):
            if a == "--recursive":
                recursive = True
            continue
        if not end_opts and a.startswith("-") and len(a) > 1:
            if "r" in a[1:] or "R" in a[1:]:
                recursive = True
            continue
        targets.append(a)
    return targets if recursive else None


def _mcp_match(m: Matchers, ctx: CallContext) -> bool:
    parts = ctx.mcp_parts()
    if parts is None:
        return False
    server, tool = parts
    if server:
        if m.mcp_server is not None and not m.mcp_server.search(server):
            return False
        return m.mcp_tool is None or bool(m.mcp_tool.fullmatch(tool))
    # Legacy name: try every split point.
    name = tool
    pieces = name.split("_")
    for k in range(1, len(pieces)):
        srv, tl = "_".join(pieces[:k]), "_".join(pieces[k:])
        if (m.mcp_server is None or m.mcp_server.search(srv)) and (m.mcp_tool is None or m.mcp_tool.fullmatch(tl)):
            return True
    return False


def _matchers_hit(m: Matchers, ctx: CallContext) -> bool:
    """All present matcher keys must match."""
    if m.tool and not any(fnmatch.fnmatchcase(ctx.tool_lower, t) for t in m.tool):
        return False
    if m.has_mcp and not _mcp_match(m, ctx):
        return False
    if m.uses_shell() and not m.tool and ctx.tool_lower not in TERMINAL_TOOLS:
        return False
    if m.command:
        if not any(p.search(s.normalized) for s in ctx.segments for p in m.command):
            return False
    if m.command_text:
        if not any(p.search(ctx.command) for p in m.command_text):
            return False
    if m.rm_allow is not None:
        hit = False
        for s in ctx.segments:
            targets = _rm_recursive_targets(s)
            if targets is None:
                continue
            if not targets or any(not _glob_match(t, m.rm_allow, basename=False) for t in targets):
                hit = True
                break
        if not hit:
            return False
    if m.writes_path:
        if not any(_glob_match(t, m.writes_path) for t in ctx.write_targets()):
            return False
    for key, pat in m.args.items():
        v = ctx.args.get(key)
        if v is None:
            return False
        sv = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
        if not pat.search(sv):
            return False
    if m.args_text and not any(p.search(ctx.args_text) for p in m.args_text):
        return False
    if m.code_text and not any(p.search(t) for t in ctx.code_texts for p in m.code_text):
        return False
    if m.undecidable is not None and ctx.undecidable != m.undecidable:
        return False
    if m.touches_path and not any(_glob_match(t, m.touches_path) for t in ctx.touch_targets()):
        return False
    if m.page_url:
        urls = [ctx.page_url] + [v for k, v in ctx.args.items() if k in ("url", "page_url") and isinstance(v, str)]
        if not any(p.search(u) for u in urls if u for p in m.page_url):
            return False
    return True


@dataclass
class Decision:
    decision: str
    rule: Optional[Rule]
    matched: List[Tuple[Rule, str]]
    mode: str
    profile: str

    @property
    def rule_id(self) -> Optional[str]:
        return self.rule.id if self.rule else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision, "mode": self.mode, "profile": self.profile,
            "rule": self.rule.to_dict() if self.rule else None,
            "matched": [{"id": r.id, "decision": d, "description": r.description} for r, d in self.matched],
        }


def _builtin_rule(rid: str, decision: str, description: str, tags: Iterable[str] = ()) -> Rule:
    return Rule(id=rid, decision=decision, description=description, when=Matchers(), unless=None,
                tags=list(tags), origin="builtin")


TOO_LONG_RULE = _builtin_rule(
    "command-too-long", "ASK",
    "command too long to evaluate: the policy only checks commands and code up to "
    f"{MAX_EVAL_CHARS // 1024} KB, so a human has to look at this one")


# Scout jobs (a bot's daily read-only digest) only get the kanban and web toolsets. Defense in depth:
# anything that runs code, writes files, sends, or drives a browser or screen is refused outright.
SCOUT_RULE = _builtin_rule(
    "scout-read-only", "BLOCK",
    "a Scout run is read-only: only kanban and web tools are allowed, this tool is not", tags=["scout"])
SCOUT_BLOCKED_TOOLS = (
    "terminal", "process*", "close_terminal", "execute_code", *WRITE_TOOLS, "send_message", "react_to_message",
    "browser_*", "computer_use", "mcp_*", "connectors__*", "cronjob*", "delegate_task", "skill_manage",
)


def scout_decision(tool_name: str, mode: str, profile: str) -> Optional[Decision]:
    name = (tool_name or "").lower()
    if any(fnmatch.fnmatchcase(name, g) for g in SCOUT_BLOCKED_TOOLS):
        return Decision("BLOCK", SCOUT_RULE, [(SCOUT_RULE, "BLOCK")], mode, profile)
    return None


def evaluate(policy: Policy, tool_name: str, args: Any, profile: str = "default", *, page_url: str = "",
             skip_tags: Iterable[str] = ()) -> Decision:
    """Decide one call. ``page_url`` is the browser page the call acts on when the caller knows it;
    rules carrying any tag in ``skip_tags`` are left out (e.g. ``bot-screen`` while a human holds the
    screen, because the engine already refuses every agent action then)."""
    ctx = CallContext(tool_name=str(tool_name or ""), args=args if isinstance(args, dict) else {},
                      page_url=page_url or "")
    mode = policy.mode_for(profile)
    if ctx.primary_text_len() > MAX_EVAL_CHARS:
        return Decision("ASK", TOO_LONG_RULE, [(TOO_LONG_RULE, "ASK")], mode, profile)
    skip = set(skip_tags or ())
    matched: List[Tuple[Rule, str]] = []
    for rule, decision in policy.rules_for(profile):
        if skip and skip.intersection(rule.tags):
            continue
        if not _matchers_hit(rule.when, ctx):
            continue
        if rule.unless is not None and not rule.unless.is_empty() and _matchers_hit(rule.unless, ctx):
            continue
        matched.append((rule, decision))
    best: Optional[Tuple[Rule, str]] = None
    for rule, decision in matched:
        if best is None or SEVERITY[decision] > SEVERITY[best[1]]:
            best = (rule, decision)
    if best is None:
        return Decision(policy.default_for(profile), None, matched, mode, profile)
    return Decision(best[1], best[0], matched, mode, profile)


def combine(decisions: List[Decision]) -> Decision:
    """Stricter decision wins; enforce wins over observe; ties keep the first (the root policy)."""
    best = decisions[0]
    for d in decisions[1:]:
        if SEVERITY[d.decision] > SEVERITY[best.decision]:
            best = d
    mode = "enforce" if any(d.mode == "enforce" for d in decisions) else "observe"
    matched: List[Tuple[Rule, str]] = []
    seen: set = set()
    for d in decisions:
        for r, dec in d.matched:
            if (r.id, r.origin) not in seen:
                seen.add((r.id, r.origin))
                matched.append((r, dec))
    return Decision(best.decision, best.rule, matched, mode, best.profile)


# ---------------------------------------------------------------------------------------------
# Paths, caching, logging
# ---------------------------------------------------------------------------------------------

def root_home() -> Path:
    override = os.environ.get("SYNTHWAVE_POLICY_ROOT", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    try:
        from hermes_constants import get_default_hermes_root  # type: ignore
        return Path(get_default_hermes_root())
    except Exception:
        return Path.home() / ".hermes"


def current_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(os.path.expanduser(val)) if val else Path.home() / ".hermes"


def profile_for_home(home: Path) -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name  # type: ignore
        name = get_active_profile_name()
        if name:
            return str(name)
    except Exception:
        pass
    try:
        resolved = home.expanduser().resolve()
        if resolved == root_home().expanduser().resolve():
            return "default"
        if resolved.parent.name == "profiles":
            return resolved.name
    except Exception:
        pass
    return "default"


ENFORCED_MARKER = ".enforced"


def root_policy_path() -> Path:
    return root_home() / "policy" / "autonomy.yaml"


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.expanduser().resolve() == b.expanduser().resolve()
    except Exception:
        return str(a) == str(b)


def profile_policy_path(home: Path) -> Optional[Path]:
    """``$HERMES_HOME/policy/autonomy.yaml`` for a profile home; None for the root home itself."""
    if _same_dir(home, root_home()):
        return None
    return home / "policy" / "autonomy.yaml"


def policy_path_for(home: Path) -> Optional[Path]:
    """The authoritative file: the root ``~/.hermes/policy/autonomy.yaml``; a profile file only when
    there is no root file (it can only tighten, see ``load_effective``)."""
    root = root_policy_path()
    if root.exists():
        return root
    own = profile_policy_path(home)
    if own is not None and own.exists():
        return own
    return None


def marker_paths(home: Path) -> List[Path]:
    out = [home / "policy" / ENFORCED_MARKER]
    root_marker = root_home() / "policy" / ENFORCED_MARKER
    if not _same_dir(root_marker.parent, out[0].parent):
        out.append(root_marker)
    return out


def enforced_marker(home: Path) -> Optional[Path]:
    """The marker that shows enforce was active for this home (or the root) at some point."""
    for p in marker_paths(home):
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def marker_sources(home: Path) -> Dict[str, Path]:
    """Policy files that were enforcing when a marker was written: {"root"|"profile": marker path}.
    An unreadable marker counts as the root file."""
    out: Dict[str, Path] = {}
    for p in marker_paths(home):
        try:
            if not p.exists():
                continue
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            sources = data.get("sources") if isinstance(data, dict) else None
        except Exception:
            sources = None
        if not isinstance(sources, list) or not sources:
            sources = ["root"]
        for src in sources:
            if src in ("root", "profile"):
                # A root-level marker's "profile" entry is the root home itself.
                out.setdefault(src if p.parent == home / "policy" else "root", p)
    return out


def write_enforced_marker(home: Path, profile: str, sources: Iterable[str] = ("root",)) -> None:
    """Best effort: remember which policy files were enforcing, so a later missing or broken file
    fails closed instead of silently switching the guard off. Removing it is a human-only step."""
    path = home / "policy" / ENFORCED_MARKER
    wanted = sorted(set(sources))
    current: List[str] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            current = sorted(set(data.get("sources") or [])) if isinstance(data, dict) else []
        except Exception:
            return  # never rewrite a marker we cannot read
        if set(wanted) <= set(current):
            return
    merged = sorted(set(current) | set(wanted))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{ENFORCED_MARKER}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps({"since": now_iso(), "profile": profile, "sources": merged}).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


class PolicyCache:
    """mtime-keyed cache so the hook re-reads the YAML only when it changes. Keeps the last policy
    that loaded cleanly per file, so a later parse error can still tell which profiles enforce."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[str, Tuple[Tuple[float, int, int], Any]] = {}
        self.last_good: Dict[str, Policy] = {}

    @property
    def last_good_mode(self) -> Dict[str, str]:  # backwards compatible view
        return {k: v.mode for k, v in self.last_good.items()}

    def last_good_mode_for(self, path: Path, profile: str) -> Optional[str]:
        pol = self.last_good.get(str(path))
        return pol.mode_for(profile) if pol else None

    def get(self, path: Path) -> Policy:
        st = path.stat()
        key = (st.st_mtime, st.st_size, st.st_ino)
        with self._lock:
            hit = self._entries.get(str(path))
            if hit and hit[0] == key:
                if isinstance(hit[1], PolicyError):
                    raise hit[1]
                return hit[1]
        try:
            pol: Any = load_policy_file(path)
        except PolicyError as exc:
            pol = exc
        with self._lock:
            self._entries[str(path)] = (key, pol)
            if isinstance(pol, Policy):
                self.last_good[str(path)] = pol
        if isinstance(pol, PolicyError):
            raise pol
        return pol


@dataclass
class Effective:
    """What applies to one profile right now.

    ``policies``: the root policy, then the profile's own file with its ``profiles:`` key ignored
    (per-profile settings only come from the root file). Every policy is evaluated and the strictest
    decision wins, so a profile file can only tighten. ``fail_closed`` is set when every call must be
    refused: a policy file is broken or the root file is missing while enforce is (or was) active.
    """

    policies: List[Tuple[Path, Policy]]
    errors: List[Tuple[Path, Exception]]
    mode: str
    fail_closed: Optional[str] = None
    marker: Optional[Path] = None

    @property
    def inert(self) -> bool:
        return not self.policies and self.fail_closed is None

    def enforcing_sources(self, profile: str) -> List[str]:
        root = root_policy_path()
        return ["root" if path == root else "profile" for path, pol in self.policies
                if pol.mode_for(profile) == "enforce"]

    def evaluate(self, tool_name: str, args: Any, profile: str, **kw: Any) -> Decision:
        decisions = [evaluate(p, tool_name, args, profile, **kw) for _, p in self.policies]
        if not decisions:
            return Decision("AUTO", None, [], self.mode, profile)
        d = combine(decisions)
        d.mode = self.mode
        return d


def load_effective(home: Path, profile: str, cache: PolicyCache) -> Effective:
    root_p = root_policy_path()
    prof_p = profile_policy_path(home)
    marker = enforced_marker(home)
    policies: List[Tuple[Path, Policy]] = []
    errors: List[Tuple[Path, Exception]] = []
    enforce_hint = False
    for path, is_profile in ((root_p, False), (prof_p, True)):
        if path is None or not path.exists():
            continue
        try:
            pol = cache.get(path)
            if is_profile and pol.profiles:
                pol = Policy(mode=pol.mode, default_decision=pol.default_decision, rules=pol.rules, profiles={},
                             max_bytes=pol.max_bytes, keep=pol.keep, source=pol.source)
            policies.append((path, pol))
        except Exception as exc:
            errors.append((path, exc))
            last = cache.last_good.get(str(path))
            if raw_mode_hint(path) == "enforce" or (last is not None and (
                    last.mode_for(profile) == "enforce" or (is_profile and last.mode == "enforce"))):
                enforce_hint = True
    mode = "enforce" if (enforce_hint or any(p.mode_for(profile) == "enforce" for _, p in policies)) else "observe"
    eff = Effective(policies=policies, errors=errors, mode=mode, marker=marker)
    if errors and (mode == "enforce" or marker is not None):
        eff.mode = "enforce"
        path, exc = errors[0]
        eff.fail_closed = (f"the policy file {path} could not be loaded ({exc}). The policy is in enforce mode, "
                           "so every tool call is refused until the file is fixed or switched to mode: observe.")
    else:
        sources = marker_sources(home) if marker is not None else {}
        for src, path in (("root", root_p), ("profile", prof_p)):
            if src in sources and (path is None or not path.exists()):
                eff.mode = "enforce"
                eff.fail_closed = (
                    f"the policy file {path or root_p} is missing while enforce was active (marker {sources[src]}). "
                    "Every tool call is refused until a human restores the file, or removes the marker to "
                    "switch the policy off on purpose.")
                break
    return eff


_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)((?:password|passwd|pwd|token|secret|api[_-]?key|apikey|authorization|auth|"
                r"access[_-]?key|private[_-]?key|client[_-]?secret|x-api-key)\s*[:=]\s*)(['\"]?)[^\s'\",}]+"),
     r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(--(?:password|token|secret|api-key|key)[= ])\S+"), r"\1[REDACTED]"),
    (re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{8,}"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"), "[REDACTED_KEY]"),
    (re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}"), "[REDACTED_LONG]"),
]


def redact(text: str, limit: int = 400) -> str:
    out = text or ""
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    if len(out) > limit:
        out = out[:limit] + "...[truncated]"
    return out


_SUMMARY_KEYS = ("command", "path", "file_path", "target", "action", "to", "recipient", "recipients",
                 "project_id", "projectId", "calendar_id", "calendarId", "campaign_id", "campaignId",
                 "thread_id", "threadId", "space", "url")


def summarize_args(tool_name: str, args: Any) -> Dict[str, Any]:
    """Small, redacted view of the arguments. Never includes message bodies or file contents."""
    if not isinstance(args, dict):
        return {}
    out: Dict[str, Any] = {"keys": sorted(str(k) for k in args)[:30]}
    for k in _SUMMARY_KEYS:
        if k in args and args[k] is not None:
            v = args[k]
            sv = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
            out[k] = redact(sv, 300)
    return out


_LOG_LOCK = threading.Lock()


def append_jsonl(path: Path, record: Dict[str, Any], max_bytes: int = DEFAULT_MAX_BYTES,
                 keep: int = DEFAULT_KEEP) -> None:
    """Append one JSON line, rotating at ``max_bytes`` (``decisions.jsonl.1`` ... ``.<keep>``)."""
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with _LOG_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(line.encode("utf-8")) > max_bytes:
            if keep <= 0:
                path.unlink(missing_ok=True)
            else:
                for idx in range(keep, 0, -1):
                    src = path if idx == 1 else path.with_name(f"{path.name}.{idx - 1}")
                    dst = path.with_name(f"{path.name}.{idx}")
                    if src.exists():
                        os.replace(src, dst)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)


def approval_rule_key(rule_id: str, tool_name: str, args: Any) -> str:
    """Approval grain: one exact call. ``[a]lways`` on an outbound send must never whitelist
    every future send of that kind, so the key includes a hash of the tool and arguments."""
    try:
        blob = json.dumps({"t": tool_name, "a": args}, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        blob = f"{tool_name}:{args!r}"
    return f"synthwave-policy:{rule_id}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
