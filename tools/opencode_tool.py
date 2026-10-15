"""opencode_task: hand a bounded repository job to OpenCode, under governance.

One call replaces the five terminal steps a model needed before (worktree,
key, model flag, background run, diff). The job runs in the background and
the completion notification carries the review report: diff stat, patch,
test output and OpenCode's own output tail.

Governance (15-10-2026, Michael):

* the tool rides the terminal grant plus the ``opencode`` CLI grant
  (``tool_policy._decide_opencode_tool_access``), so an account that may not
  run ``opencode run`` through terminal never sees this tool;
* workdir, task text and test command are checked like terminal arguments
  (``tool_policy._opencode_argument_decision``) by the dispatcher, and again
  here against the live context, failing closed when the context cannot be
  resolved;
* the profile folderguard checks the workdir;
* OpenCode itself runs confined (see ``tools/opencode_task_runner.py``):
  read-only filesystem outside the job's worktree, secret locations and the
  caller's denied paths masked, per-job OpenCode state, global slot limit.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

TOOL_NAME = "opencode_task"
DEFAULT_MODEL = "omniroute/auto/best-coding"
MAX_SLOTS = 4
RUNNER = Path(__file__).resolve().with_name("opencode_task_runner.py")

# Pinned on the passwd home: under the profile multiplexer $HOME points at
# ~/.hermes-mux/home, and a guard that reads the wrong home silently does
# nothing (see the synthpulse-governance skill, "De stille storing").
_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
OPENCODE_BIN = os.environ.get("HERMES_OPENCODE_BIN") or str(
    _HOME / ".npm-global" / "lib" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe")
JOBS_DIR = _HOME / ".hermes" / "cache" / "opencode-tasks"
SLOT_DIR = _HOME / ".hermes" / "run" / "opencode-slots"

# Masked for everyone inside the sandbox, owners included: credentials and
# other people's state are never part of a coding job.
ALWAYS_MASK_DIRS = (
    ".hermes", ".hermes-mux", ".ssh", ".gnupg", ".claude", ".codex", ".aws", ".azure",
    ".azure-devops", ".kube", ".docker", ".config/gcloud", ".config/gh", ".config/bunq",
    ".config/wealth-automation", ".local/share/opencode", ".IdentityService", ".apify",
    "bunq-agentic", "ObsidianVault/.obsidian",
)
ALWAYS_MASK_FILES = (
    ".env", ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".claude.json",
    ".config/opencode/opencode.jsonc", ".config/opencode/opencode.json", ".config/opencode/omniroute.key",
)
CLIENT_CREDS_DIR = ".config/synthwave/clients"

# OpenCode's own permission layer: a second lock behind the sandbox, and the
# only one that speaks to OpenCode's read/edit tools by pattern.
SECRET_PATTERNS = ("**/.env", "**/.env.*", "**/.env*", "**/.hermes/**", "**/secrets/**",
                   "**/*.pem", "**/*.key", "**/credentials.json", "**/auth.json",
                   "**/.config/synthwave/clients/**")

OPENCODE_TASK_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Hand a bounded repository job (feature, refactor, tests, review, script) to OpenCode, "
        "a coding agent, and get a review report back. Runs in the background in its own git "
        "worktree; you are notified with the diff stat, patch, test output and OpenCode's output "
        "when it finishes. Use it for code work that does not need your session context, tools or "
        "access; run several in parallel (one per task). Review the diff before merging. "
        "OpenCode cannot read secrets or files outside the workdir."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Self-contained instructions: goal, files, constraints, how to verify."},
            "workdir": {"type": "string", "description": "Absolute path of the repository or folder to work in."},
            "model": {"type": "string", "description": f"provider/model for OpenCode (default {DEFAULT_MODEL})."},
            "background": {"type": "boolean", "description": "Run in the background and notify on completion (default true)."},
            "test_command": {"type": "string", "description": "Optional command run in the worktree after OpenCode, e.g. 'pytest -q tests/x'."},
            "timeout": {"type": "integer", "description": "Seconds before OpenCode is stopped (default 3600, max 7200)."},
        },
        "required": ["task", "workdir"],
    },
}


def _current_context():
    try:
        from hermes_cli.dashboard_governance.context import current_governance_context
        return current_governance_context(), None
    except Exception as exc:  # noqa: BLE001
        return None, exc


def _governed_without_context() -> bool:
    """True when this process should have a governance context but has none."""
    if os.environ.get("HERMES_DASHBOARD_GOVERNANCE_CONTEXT"):
        return True
    try:
        from tools import profile_scope_bridge
        mod = profile_scope_bridge.load_profile_scope()
        if mod is not None and mod.is_scoped(mod.resolve_profile()):
            return True
    except Exception:  # noqa: BLE001
        return True
    return False


def check_governance(args: Dict[str, Any]):
    """(context, error). Fail closed whenever the caller's access cannot be resolved."""
    ctx, exc = _current_context()
    if exc is not None:
        return None, "Blocked by governance: access could not be resolved (fail-closed)."
    if ctx is None:
        if _governed_without_context():
            return None, "Blocked by governance: no governance context for a governed profile (fail-closed)."
        return None, None
    try:
        from hermes_cli.dashboard_governance.tool_policy import (
            tool_allowed_for_context, tool_arguments_allowed_for_context,
        )
        tool = tool_allowed_for_context(ctx, TOOL_NAME, registry)
        if not tool.allowed:
            return ctx, f"Blocked by governance: {tool.reason}."
        decision = tool_arguments_allowed_for_context(ctx, TOOL_NAME, args)
        if not decision.allowed:
            detail = f" ({decision.detail})" if getattr(decision, "detail", "") else ""
            return ctx, f"Blocked by governance: {decision.reason}{detail}."
    except Exception:  # noqa: BLE001
        logger.debug("opencode_task governance check failed", exc_info=True)
        return ctx, "Blocked by governance: policy check failed (fail-closed)."
    return ctx, None


def check_folderguard(workdir: str) -> Optional[str]:
    try:
        from tools import profile_scope_bridge
        mod = profile_scope_bridge.load_profile_scope()
    except Exception:  # noqa: BLE001
        mod = None
    if mod is None:
        return "Blocked: profile scope guard unavailable (fail-closed)." if _governed_without_context() else None
    try:
        profile = mod.resolve_profile()
        ok, reason = mod.check_path(profile, workdir, "write")
    except Exception:  # noqa: BLE001
        return "Blocked: profile scope check failed (fail-closed)."
    return None if ok else f"Blocked by folder scope: {reason}"


def _git_top(workdir: Path) -> Optional[Path]:
    try:
        done = subprocess.run(["git", "-C", str(workdir), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return Path(done.stdout.strip())


def _literal_path(glob: str) -> Optional[str]:
    """A denied glob that names one concrete place (optionally with /**)."""
    raw = os.path.expanduser(str(glob).strip())
    for suffix in ("/**", "/*"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
    if not raw.startswith("/") or any(ch in raw for ch in "*?["):
        return None
    return raw.rstrip("/") or None


def mask_plan(access) -> tuple[list[str], list[str], list[str]]:
    """(dirs to mask, files to mask, OpenCode deny patterns) for this caller."""
    home = str(_HOME)
    dirs = [os.path.join(home, d) for d in ALWAYS_MASK_DIRS]
    files = [os.path.join(home, f) for f in ALWAYS_MASK_FILES]
    patterns = list(SECRET_PATTERNS)
    creds = os.path.join(home, CLIENT_CREDS_DIR)
    creds_allowed = False
    if access is None:
        creds_allowed = False
    else:
        try:
            from hermes_cli.dashboard_governance.tool_policy import decide_tool_argument_access
            creds_allowed = decide_tool_argument_access(
                access, "read_file", {"path": os.path.join(creds, "probe", "creds.json")}).allowed
        except Exception:  # noqa: BLE001
            creds_allowed = False
    if creds_allowed:
        patterns.remove("**/.config/synthwave/clients/**")
    else:
        dirs.append(creds)
    if access is not None:
        globs = set(getattr(access.grants, "file_denied_globs", ()) or ()) | set(
            getattr(access.deny, "file_denied_globs", ()) or ())
        for glob in sorted(globs):
            literal = _literal_path(glob)
            if literal:
                (dirs if Path(literal).is_dir() or glob.endswith(("/**", "/*")) else files).append(literal)
            elif str(glob).startswith("**/") or str(glob).startswith("*"):
                patterns.append(str(glob))
    return sorted(set(dirs)), sorted(set(files)), list(dict.fromkeys(patterns))


RUNTIME_PATHS = (".npm-global", ".config/opencode", ".local/bin", ".gitconfig", ".bun")


def home_layout(access) -> dict:
    """Blacklist accounts and the ungoverned owner see the home read-only with
    secrets masked. Whitelist accounts see an empty home with only their own
    read roots mounted back: what they cannot read through Hermes, OpenCode
    cannot read either."""
    if access is None or getattr(access, "access_mode", "") == "blacklist":
        return {"home_mode": "masked"}
    roots = sorted(str(Path(os.path.expanduser(r))) for r in (getattr(access.grants, "file_read_roots", ()) or ())
                   if r and r != "*" and not any(ch in str(r) for ch in "*?["))
    return {"home_mode": "allowlist", "read_roots": roots,
            "runtime_paths": [str(_HOME / p) for p in RUNTIME_PATHS]}


def opencode_config(deny_patterns: list[str]) -> dict:
    """Inline config (highest precedence): provider, no MCP, no reach outside the workdir."""
    rules = {"*": "allow"}
    for pattern in deny_patterns:
        rules[pattern] = "deny"
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "omniroute": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "OmniRoute (Synthwave gateway)",
                "options": {"baseURL": "http://127.0.0.1:20128/v1", "apiKey": "{env:OMNIROUTE_API_KEY}"},
                "models": {m: {"name": m} for m in (
                    "auto/best-coding", "auto/coding", "auto/best-coding-fast", "auto/best-reasoning")},
            }
        },
        "mcp": {},
        "permission": {
            "external_directory": "deny",
            "read": dict(rules),
            "edit": dict(rules),
            "bash": {"*": "allow", "*.env*": "deny", "*/.hermes/*": "deny", "*secrets*": "deny",
                     "*opencode*run*": "deny", "*.config/synthwave/clients*": "deny"},
            "webfetch": "allow",
        },
    }


def opencode_task(task: str, workdir: str, model: Optional[str] = None, background: bool = True,
                  test_command: Optional[str] = None, timeout: Optional[int] = None,
                  task_id: Optional[str] = None, session_id: Optional[str] = None) -> str:
    task = (task or "").strip()
    if not task:
        return tool_error("opencode_task needs a task.")
    if os.environ.get("OPENCODE_RECURSION_GUARD"):
        return tool_error("Refused: already running inside OpenCode (recursion guard). Do the work directly.")
    raw_workdir = (workdir or "").strip()
    if not raw_workdir or not os.path.isabs(os.path.expanduser(raw_workdir)):
        return tool_error("workdir must be an absolute path.")
    resolved = Path(os.path.realpath(os.path.expanduser(raw_workdir)))
    if not resolved.is_dir():
        return tool_error(f"workdir does not exist: {resolved}")
    args = {"task": task, "workdir": str(resolved), "test_command": test_command or ""}
    ctx, blocked = check_governance(args)
    if blocked:
        return tool_error(blocked)
    blocked = check_folderguard(str(resolved))
    if blocked:
        return tool_error(blocked)
    if not Path(OPENCODE_BIN).is_file():
        return tool_error(f"OpenCode binary not found at {OPENCODE_BIN}.")
    # The runner is a Hermes control-plane process on the host. A profile whose
    # terminal is sandboxed (docker, ssh) must not gain a host process this way.
    try:
        from tools.terminal_tool import _get_env_config
        env_type = _get_env_config().get("env_type", "local")
    except Exception:  # noqa: BLE001
        env_type = "unknown"
    if env_type != "local":
        return tool_error(f"opencode_task needs the host terminal backend; this profile uses '{env_type}'.")

    access = getattr(ctx, "access", None) if ctx is not None else None
    top = _git_top(resolved)
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    job: Dict[str, Any] = {
        "id": job_id, "task": task, "workdir": str(resolved), "model": (model or DEFAULT_MODEL).strip(),
        "opencode_bin": OPENCODE_BIN, "home": str(_HOME), "slot_dir": str(SLOT_DIR), "slots": MAX_SLOTS,
        "slot_wait": 600, "timeout": max(60, min(int(timeout or 3600), 7200)),
        "test_command": (test_command or "").strip(),
        "path": ":".join([str(_HOME / ".npm-global" / "bin"), str(_HOME / ".local" / "bin"),
                          "/usr/local/bin", "/usr/bin", "/bin"]),
        "owner_email": getattr(getattr(access, "subject", None), "email", "") if access is not None else "",
    }
    if top is not None:
        if access is not None:
            from hermes_cli.dashboard_governance.tool_policy import decide_tool_argument_access
            probe = decide_tool_argument_access(access, "write_file", {"path": str(top / ".opencode-worktrees" / "probe")})
            if not probe.allowed:
                return tool_error(f"Blocked by governance: the repository root {top} is outside your write scope.")
        job.update({"git_top": str(top), "branch": f"opencode/{job_id}",
                    "worktree": str(top / ".opencode-worktrees" / job_id)})
    mask_dirs, mask_files, deny_patterns = mask_plan(access)
    # Never mask the place OpenCode has to work in.
    keep = Path(job.get("worktree") or resolved)
    job["mask_dirs"] = [d for d in mask_dirs if not str(keep).startswith(d.rstrip("/") + "/") and str(keep) != d]
    job["mask_files"] = mask_files
    job["opencode_config"] = opencode_config(deny_patterns)
    job.update(home_layout(access))

    run_dir = JOBS_DIR / job_id
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (run_dir / "job.json").write_text(json.dumps(job, indent=1))
    key = os.environ.get("OMNIROUTE_API_KEY", "")
    if key:
        key_path = run_dir / "omniroute.key"
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key)

    command = f"{shlex.quote(sys.executable)} -I {shlex.quote(str(RUNNER))} {shlex.quote(str(run_dir))}"
    from tools.terminal_tool import terminal_tool
    result = terminal_tool(command=command, background=bool(background), timeout=None if background else job["timeout"] + 900,
                           task_id=task_id, session_id=session_id, workdir=str(resolved),
                           notify_on_complete=bool(background), _host_local=True,
                           _completion_output_chars=40000)
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        data = {"output": result}
    data.update({"opencode_job": job_id, "model": job["model"],
                 "worktree": job.get("worktree", ""), "branch": job.get("branch", "")})
    if background:
        data["note"] = ("OpenCode is working in the background; you will be notified with the review report. "
                        "Review the diff before merging the branch.")
    return json.dumps(data)


def check_opencode_requirements() -> bool:
    return Path(OPENCODE_BIN).is_file() and RUNNER.is_file()


def _handle_opencode_task(args, **kw):
    return opencode_task(
        task=args.get("task"), workdir=args.get("workdir"), model=args.get("model"),
        background=args.get("background", True), test_command=args.get("test_command"),
        timeout=args.get("timeout"), task_id=kw.get("task_id"), session_id=kw.get("session_id"),
    )


registry.register(
    name=TOOL_NAME,
    toolset="delegation",
    schema=OPENCODE_TASK_SCHEMA,
    handler=_handle_opencode_task,
    check_fn=check_opencode_requirements,
    emoji="🧑‍💻",
    max_result_size_chars=60_000,
)
