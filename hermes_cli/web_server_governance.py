"""SynthPulse dashboard governance: web-layer helpers shared by the governance router and the
upstream route modules that enforce it (model picker/assignment, chat PTY spawn).

Extracted from the fork's monolithic ``hermes_cli.web_server`` additions when upstream split
that module into ``web_server_*`` helpers and ``web_routers/*``. The policy model, resolver,
loader and audit sink live in :mod:`hermes_cli.dashboard_governance`; this module only adapts
them to FastAPI requests and WebSockets.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import HTTPException, Request

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import WebSocket

# Same logger the handlers used before extraction (identical logger object).
_log = logging.getLogger("hermes_cli.web_server")


# ---------------------------------------------------------------------------
# Request principal
# ---------------------------------------------------------------------------


def request_governance_access(request: Request):
    """Effective access for this request: the middleware-resolved one, else resolve now."""
    from hermes_cli.dashboard_governance.enforcement import effective_access_for_request

    return getattr(request.state, "governance_access", None) or effective_access_for_request(request)


def require_governance_admin(request: Request) -> None:
    """Defense-in-depth gate for policy-mutating governance endpoints.

    The governance middleware only hard-denies in ``enforce`` mode: in ``report_only`` (the
    documented Stage 1 rollout mode) denied requests are audited as ``would_deny`` and still
    reach the handler, and with mode ``off`` / no policy file every request passes with reason
    ``governance_off``. Policy mutations must never execute for subjects without
    ``governance:write`` regardless of mode, otherwise any authenticated session-holder could
    persist grants for themselves during a dry-run rollout (or bootstrap a policy file that
    flips mode to enforce).
    """
    if not request_governance_access(request).has_permission("governance:write"):
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Policy read-modify-write helpers
# ---------------------------------------------------------------------------


def governance_policy_etag(raw: Any) -> str:
    """Content hash of a policy payload for optimistic-concurrency checks."""
    payload = raw if isinstance(raw, dict) else {}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def governance_policy_summary(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    users = data.get("users") if isinstance(data.get("users"), dict) else {}
    groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
    roles = data.get("roles") if isinstance(data.get("roles"), dict) else {}
    return {
        "version": data.get("version"),
        "mode": str(data.get("mode") or ""),
        "users": sorted(str(key) for key in users),
        "groups": sorted(str(key) for key in groups),
        "roles": sorted(str(key) for key in roles),
    }


def audit_governance_policy_change(
    request: Request, *, action: str, target: str = "", before: Any = None, after: Any = None,
) -> None:
    try:
        from hermes_cli.dashboard_governance.audit import append_audit_event

        access = request_governance_access(request)
        append_audit_event(
            "policy_change",
            subject_email=access.subject.email,
            subject_user_id=access.subject.user_id,
            path=request.url.path,
            method=request.method,
            reason=action,
            mode=access.mode,
            extra={"action": action, "target": target, "before": before, "after": after},
        )
    except Exception:
        # Policy writes must not fail because the audit sink is temporarily unwritable; the
        # mutation itself has already been persisted.
        return


def validated_governance_entry(raw_entry: Any, *, kind: str) -> Dict[str, Any]:
    if raw_entry is None:
        return {}
    if not isinstance(raw_entry, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_payload", "message": f"{kind} entry must be an object"},
        )
    for field_name in ("roles", "groups", "sso_groups"):
        value = raw_entry.get(field_name)
        if value is not None and not isinstance(value, list):
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_payload", "message": f"{kind} {field_name} must be a list"},
            )
    grants = raw_entry.get("grants")
    if grants is not None and not isinstance(grants, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_payload", "message": f"{kind} grants must be an object"},
        )
    return dict(raw_entry)


def governance_raw_policy(request: Request) -> Dict[str, Any]:
    from hermes_cli.dashboard_governance.enforcement import policy_for_request

    return dict(policy_for_request(request).raw or {})


def save_governance_raw_policy(raw: Dict[str, Any], *, label: str) -> Path:
    from hermes_cli.dashboard_governance.loader import GovernancePolicyError, save_governance_policy

    try:
        return save_governance_policy(raw)
    except GovernancePolicyError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_policy", "message": str(exc)})
    except Exception:
        _log.exception("%s failed", label)
        raise HTTPException(status_code=500, detail="Internal server error")


def subject_from_payload(raw_subject: Dict[str, Any]):
    """Build a hypothetical :class:`GovernanceSubject` for preview/simulate requests."""
    from hermes_cli.dashboard_governance.models import GovernanceSubject

    return GovernanceSubject(
        email=str(raw_subject.get("email") or ""),
        display_name=str(raw_subject.get("display_name") or raw_subject.get("name") or ""),
        provider=str(raw_subject.get("provider") or ""),
        user_id=str(raw_subject.get("user_id") or ""),
        org_id=str(raw_subject.get("org_id") or ""),
        roles=tuple(str(role) for role in raw_subject.get("roles") or [] if str(role).strip()),
        groups=tuple(str(group) for group in raw_subject.get("groups") or [] if str(group).strip()),
    )


# ---------------------------------------------------------------------------
# Model governance (GET /api/model/options, POST /api/model/set)
# ---------------------------------------------------------------------------


def filter_model_options_for_request(payload: dict, request: Optional[Request]) -> dict:
    """Drop providers/models the request's principal may not use from a picker payload."""
    access = getattr(request.state, "governance_access", None) if request is not None else None
    if access is None:
        return payload
    from hermes_cli.dashboard_governance.model_policy import filter_model_options_payload

    return filter_model_options_payload(payload, access)


def ensure_model_allowed(governance_access, provider: str, model: str) -> None:
    """Raise 403 when ``governance_access`` forbids ``provider``/``model``; no-op without access."""
    if governance_access is None:
        return
    from hermes_cli.dashboard_governance.model_policy import decide_model_access

    decision = decide_model_access(governance_access, provider=provider, model=model)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail={"reason": decision.reason})


# ---------------------------------------------------------------------------
# Chat PTY governance context (/api/pty)
# ---------------------------------------------------------------------------


def dashboard_governance_context_env_for_ws(
    ws: "WebSocket", *, credential: str, profile: Optional[str], resume: Optional[str],
    channel: Optional[str],
) -> Optional[str]:
    """Serialize the accepted WS principal's effective governance for PTY children.

    Identity comes from the server-minted ``ws._hermes_auth_identity`` that
    ``web_server_chat._ws_auth_reason`` stamps for internal / ticket / provider-verified token
    credentials (never from client-supplied params). A loopback ``_SESSION_TOKEN`` connection
    carries no stamped identity and is mapped to the dashboard-token principal. Returns None
    when governance is off or the context cannot be built (the child then runs ungoverned, the
    same as before governance existed).
    """
    try:
        from hermes_cli.dashboard_governance.context import (
            DashboardGovernanceContext, serialize_context_for_env)
        from hermes_cli.dashboard_governance.loader import load_governance_policy
        from hermes_cli.dashboard_governance.models import GovernanceSubject
        from hermes_cli.dashboard_governance.resolver import resolve_effective_access

        loader = getattr(ws.app.state, "governance_policy_loader", None)
        policy = loader() if callable(loader) else load_governance_policy()
        if not getattr(policy, "enabled", False):
            return None

        info = getattr(ws, "_hermes_auth_identity", None)
        if not isinstance(info, dict):
            info = {}
        user_id = str(info.get("user_id") or "")
        provider = str(info.get("provider") or "")
        stamped = bool(user_id)
        if not user_id and credential == "token":
            user_id = "dashboard-session-token"
        if not provider and credential == "token":
            provider = "dashboard-token"
        # Dashboard token scope: the server-internal credential and the legacy loopback
        # session token. A provider-verified user session token (gated mode) is a person,
        # not the dashboard, so it resolves on its identity alone.
        token_scopes = (
            ("dashboard",) if credential == "internal" or (credential == "token" and not stamped) else ()
        )
        subject = GovernanceSubject(
            email=user_id if "@" in user_id else "",
            display_name=user_id,
            provider=provider,
            user_id=user_id,
            token_scopes=token_scopes,
        )
        access = resolve_effective_access(policy, subject)
        requested_profile = (profile or "").strip()
        active_profile = (
            requested_profile if requested_profile and requested_profile.lower() != "current" else "default"
        )
        ctx = DashboardGovernanceContext(
            subject=subject,
            access=access,
            active_profile=active_profile,
            session_id=resume or "",
            request_id=channel or "",
        )
        return serialize_context_for_env(ctx)
    except Exception:
        _log.debug("failed to build dashboard governance context for WS child", exc_info=True)
        return None


def apply_governance_context_env(env: Optional[dict], governance_context_env: Optional[str]) -> None:
    """Inject a serialized governance context into a chat child's env (no-op when absent)."""
    if not governance_context_env or env is None:
        return
    try:
        from hermes_cli.dashboard_governance.context import GOVERNANCE_CONTEXT_ENV

        env[GOVERNANCE_CONTEXT_ENV] = governance_context_env
    except Exception:
        _log.debug("Failed to inject dashboard governance context into chat env", exc_info=True)
