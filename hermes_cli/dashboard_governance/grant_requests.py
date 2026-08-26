"""Governance denial -> access-request spool.

When the tool gate denies a governed user something grantable, the denial is
recorded here as an aggregated access request. The Hermes WebUI ingests this
spool into its admin approvals queue (kind "grant"), where an admin can
approve the grant with one click. This module deliberately has no WebUI
imports: it only appends to a JSON store under the WebUI state dir.

Store shape (``~/.hermes/webui/governance-grant-requests.json``)::

    { "<email>|<gkind>|<value>": {
        "email": ..., "gkind": ..., "value": ..., "tool": ..., "reason": ...,
        "detail": ..., "count": 3, "first_seen": ts, "last_seen": ts } }
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_STORE_NAME = "governance-grant-requests.json"


def _store_path() -> Path:
    """Resolve the spool lazily from the env so a test STATE_DIR override (or
    a relocated HERMES_HOME) never writes into the live admin queue."""
    state_dir = os.environ.get("HERMES_WEBUI_STATE_DIR")
    if state_dir:
        return Path(state_dir).expanduser() / _STORE_NAME
    home = os.environ.get("HERMES_HOME") or "~/.hermes"
    return Path(home).expanduser() / "webui" / _STORE_NAME

# Denial reasons that map to a grantable request. Everything else (shell
# operators, denied globs, usage caps) is a deliberate rule, not a grant gap.
_PATH_KINDS = {"file_read", "file_write"}


def _map_denial(tool_name: str, reason: str, detail: str) -> tuple[str, str] | None:
    """Map a denial to (gkind, value), or None when it is not grantable."""
    detail = (detail or "").strip()
    if reason == "cli_command_not_allowed" and detail:
        return "cli", detail
    if reason == "skill_not_allowed" and detail:
        return "skill", detail.rsplit("/", 1)[-1]
    if reason == "cli_workdir_not_allowed" and detail:
        return "workdir", detail
    if reason == "file_read_root_not_allowed" and detail:
        return "file_read", _containing_dir(detail)
    if reason == "file_write_root_not_allowed" and detail:
        return "file_write", _containing_dir(detail)
    if reason == "mcp_server_not_allowed":
        return "mcp", detail or _mcp_server_of(tool_name) or tool_name
    if reason == "profile_not_allowed" and detail:
        return "profile", detail
    if reason == "workspace_not_allowed" and detail:
        return "workspace", detail
    if reason == "tool_not_allowed":
        server = _mcp_server_of(tool_name)
        if server:
            return "mcp", server
        return "tool", tool_name
    if reason == "file_denied_glob" and detail:
        # A secret-bearing path a governed user was blocked from. Grantable per
        # person: approving adds this exact path to their files.allow_globs, an
        # exception that overrides denied_globs for that one path only.
        return "secret_glob", detail
    if reason == "route_not_allowed" and detail:
        # An API route outside the caller's allowlist. Grantable: approving adds
        # the path to their grants.routes. The permission layer still applies,
        # so this alone never confers an admin capability.
        return "route", detail
    return None


def _mcp_server_of(tool_name: str) -> str:
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3 and parts[1]:
            return parts[1]
    return ""


def _containing_dir(path: str) -> str:
    """A file path becomes its directory; a directory-ish path stays itself."""
    base = os.path.basename(path.rstrip("/"))
    if "." in base and not base.startswith("."):
        return os.path.dirname(path) or path
    return path


def record_denial(ctx, tool_name: str, reason: str, detail: str = "") -> bool:
    """Record one governance denial as an access request. Never raises."""
    try:
        email = str(
            getattr(getattr(getattr(ctx, "access", None), "subject", None), "email", "") or ""
        ).strip().lower()
        if not email:
            return False
        mapped = _map_denial(str(tool_name or ""), str(reason or ""), str(detail or ""))
        if mapped is None:
            return False
        gkind, value = mapped
        if not value:
            return False
        key = f"{email}|{gkind}|{value}"
        now = time.time()
        store_file = _store_path()
        store_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = store_file.with_suffix(".lock")
        with open(lock_path, "w") as lock_fh:
            try:
                import fcntl

                fcntl.flock(lock_fh, fcntl.LOCK_EX)
            except Exception:
                pass  # windows / no fcntl: best-effort without the lock
            try:
                store = json.loads(store_file.read_text(encoding="utf-8"))
                if not isinstance(store, dict):
                    store = {}
            except (FileNotFoundError, ValueError):
                store = {}
            entry = store.get(key)
            if isinstance(entry, dict):
                entry["count"] = int(entry.get("count") or 0) + 1
                entry["last_seen"] = now
                entry["tool"] = str(tool_name or "")
                entry["detail"] = str(detail or "")
            else:
                store[key] = {
                    "email": email,
                    "gkind": gkind,
                    "value": value,
                    "tool": str(tool_name or ""),
                    "reason": str(reason or ""),
                    "detail": str(detail or ""),
                    "count": 1,
                    "first_seen": now,
                    "last_seen": now,
                }
            tmp = store_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, store_file)
        return True
    except Exception as exc:  # pragma: no cover: must never break tool flow
        logger.debug("grant request record failed: %s", exc)
        return False


# Admin identification, so a denial can tell the user WHO can approve it.
# Resolved from the live policy (roles owner/admin or group sw-admins) rather
# than hardcoded, so a change in the policy moves the pointer with it.
_ADMIN_ROLES = frozenset({"owner", "admin"})
_ADMIN_GROUPS = frozenset({"sw-admins"})


def approver_emails() -> list[str]:
    """Emails that can approve an access request. Empty list when unknown."""
    try:
        from hermes_cli.dashboard_governance.loader import load_governance_policy

        policy = load_governance_policy()
        out = []
        for email, user in (getattr(policy, "users", None) or {}).items():
            roles = {str(r).lower() for r in (getattr(user, "roles", None) or ())}
            groups = {str(g).lower() for g in (getattr(user, "groups", None) or ())}
            if roles & _ADMIN_ROLES or groups & _ADMIN_GROUPS:
                out.append(str(email))
        return sorted(out)
    except Exception as exc:  # pragma: no cover: never break the denial path
        logger.debug("approver lookup failed: %s", exc)
        return []


def load_store() -> dict:
    """Read the aggregated request store (empty dict when absent/corrupt)."""
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}
