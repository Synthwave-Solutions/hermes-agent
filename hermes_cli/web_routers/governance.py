"""SynthPulse dashboard governance admin routes: ``/api/governance/*``.

Effective-access probe, policy read/replace (etag optimistic concurrency), structured
group/user mutations, policy preview/simulate, audit and usage feeds. Every mutation is
serialized by the app-scoped governance-policy lock (``web_server._get_governance_policy_lock``)
and gated by ``governance:write`` regardless of the policy mode.

Extracted from the fork's additions to ``hermes_cli.web_server`` onto upstream's router layout.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from hermes_cli.web_deps import late
from hermes_cli.web_server_governance import (
    audit_governance_policy_change,
    governance_policy_etag,
    governance_policy_summary,
    governance_raw_policy,
    request_governance_access,
    require_governance_admin,
    save_governance_raw_policy,
    subject_from_payload,
    validated_governance_entry,
)

# Same logger the handlers used before extraction (identical logger object).
_log = logging.getLogger("hermes_cli.web_server")
router = APIRouter()

# Late-bound: the lock lives on app.state and its accessor on web_server (lifespan-owned).
_get_governance_policy_lock = late("_get_governance_policy_lock")

_SELF_ROUTES = frozenset({"/api/auth/me", "/api/governance/me", "/api/governance/effective-access"})


@router.get("/api/governance/effective-access")
@router.get("/api/governance/me")
async def get_governance_me(request: Request):
    from hermes_cli.dashboard_governance.enforcement import serialize_effective_access

    return {"effective_access": serialize_effective_access(request_governance_access(request))}


@router.get("/api/governance/policy")
async def get_governance_policy(request: Request):
    from hermes_cli.dashboard_governance.enforcement import (
        policy_for_request, safe_policy_payload, serialize_effective_access)

    access = request_governance_access(request)
    payload = safe_policy_payload(policy_for_request(request))
    return {
        "policy": payload,
        "etag": governance_policy_etag(payload),
        "effective_access": serialize_effective_access(access),
    }


@router.put("/api/governance/policy")
async def put_governance_policy(request: Request, body: Dict[str, Any]):
    from hermes_cli.dashboard_governance.enforcement import (
        effective_access_for_request, policy_for_request, safe_policy_payload,
        serialize_effective_access)
    from hermes_cli.dashboard_governance.loader import GovernancePolicyError, save_governance_policy

    require_governance_admin(request)
    async with _get_governance_policy_lock(request.app):
        current = safe_policy_payload(policy_for_request(request))
        # Optimistic concurrency: a client that echoes the etag it loaded (If-Match) cannot
        # silently revert entry edits that landed between its GET and this full-replace PUT.
        expected_etag = str(request.headers.get("if-match") or "").strip().strip('"')
        if expected_etag and expected_etag != governance_policy_etag(current):
            raise HTTPException(
                status_code=412,
                detail={
                    "error": "policy_conflict",
                    "message": "policy changed since it was loaded; reload and retry",
                },
            )
        before = governance_policy_summary(current)
        try:
            path = save_governance_policy(body)
        except GovernancePolicyError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception:
            _log.exception("PUT /api/governance/policy failed")
            raise HTTPException(status_code=500, detail="Internal server error")

    audit_governance_policy_change(
        request, action="policy_replace", target="policy", before=before,
        after=governance_policy_summary(body))
    # Re-resolve: the replaced policy may have changed the caller's own access.
    access = effective_access_for_request(request)
    payload = safe_policy_payload(policy_for_request(request))
    return {
        "ok": True,
        "path": str(path),
        "policy": payload,
        "etag": governance_policy_etag(payload),
        "effective_access": serialize_effective_access(access),
    }


@router.get("/api/governance/users")
async def get_governance_users(request: Request):
    raw = governance_raw_policy(request)
    users = raw.get("users") if isinstance(raw.get("users"), dict) else {}
    return {"users": users or {}}


@router.get("/api/governance/groups")
async def get_governance_groups(request: Request):
    raw = governance_raw_policy(request)
    groups = raw.get("groups") if isinstance(raw.get("groups"), dict) else {}
    return {"groups": groups or {}}


@router.post("/api/governance/groups")
async def post_governance_group(request: Request, body: Dict[str, Any]):
    require_governance_admin(request)
    name = str(body.get("name") or "").strip() if isinstance(body, dict) else ""
    if not name:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_payload", "message": "name must be a non-empty string"},
        )
    entry = validated_governance_entry(body.get("group"), kind="group")
    async with _get_governance_policy_lock(request.app):
        raw = governance_raw_policy(request)
        groups = dict(raw.get("groups")) if isinstance(raw.get("groups"), dict) else {}
        if name in groups:
            raise HTTPException(
                status_code=409,
                detail={"error": "group_exists", "message": f"group already exists: {name}"},
            )
        groups[name] = entry
        raw["groups"] = groups
        path = save_governance_raw_policy(raw, label="POST /api/governance/groups")
    audit_governance_policy_change(request, action="group_create", target=name, before=None, after=entry)
    return {"ok": True, "path": str(path), "groups": groups}


@router.put("/api/governance/groups/{name}")
async def put_governance_group(request: Request, name: str, body: Dict[str, Any]):
    require_governance_admin(request)
    group_name = str(name or "").strip()
    entry = validated_governance_entry(body, kind="group")
    async with _get_governance_policy_lock(request.app):
        raw = governance_raw_policy(request)
        groups = dict(raw.get("groups")) if isinstance(raw.get("groups"), dict) else {}
        if group_name not in groups:
            raise HTTPException(
                status_code=404,
                detail={"error": "group_not_found", "message": f"unknown group: {group_name}"},
            )
        before = groups.get(group_name)
        groups[group_name] = entry
        raw["groups"] = groups
        path = save_governance_raw_policy(raw, label="PUT /api/governance/groups/{name}")
    audit_governance_policy_change(request, action="group_update", target=group_name, before=before, after=entry)
    return {"ok": True, "path": str(path), "groups": groups}


@router.delete("/api/governance/groups/{name}")
async def delete_governance_group(request: Request, name: str):
    require_governance_admin(request)
    group_name = str(name or "").strip()
    async with _get_governance_policy_lock(request.app):
        raw = governance_raw_policy(request)
        groups = dict(raw.get("groups")) if isinstance(raw.get("groups"), dict) else {}
        if group_name not in groups:
            raise HTTPException(
                status_code=404,
                detail={"error": "group_not_found", "message": f"unknown group: {group_name}"},
            )
        before = groups.pop(group_name)
        raw["groups"] = groups
        path = save_governance_raw_policy(raw, label="DELETE /api/governance/groups/{name}")
    audit_governance_policy_change(request, action="group_delete", target=group_name, before=before, after=None)
    return {"ok": True, "path": str(path), "groups": groups}


@router.put("/api/governance/users/{email}")
async def put_governance_user(request: Request, email: str, body: Dict[str, Any]):
    from hermes_cli.dashboard_governance.models import _norm_email

    require_governance_admin(request)
    normalized = _norm_email(email)
    if not normalized or "@" not in normalized:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_payload", "message": "email must be a valid address"},
        )
    entry = validated_governance_entry(body, kind="user")
    async with _get_governance_policy_lock(request.app):
        raw = governance_raw_policy(request)
        users = dict(raw.get("users")) if isinstance(raw.get("users"), dict) else {}
        before = None
        for existing_key in list(users):
            if _norm_email(str(existing_key)) == normalized:
                before = users.pop(existing_key)
        users[normalized] = entry
        raw["users"] = users
        path = save_governance_raw_policy(raw, label="PUT /api/governance/users/{email}")
    action = "user_update" if before is not None else "user_create"
    audit_governance_policy_change(request, action=action, target=normalized, before=before, after=entry)
    return {"ok": True, "path": str(path), "users": users}


@router.delete("/api/governance/users/{email}")
async def delete_governance_user(request: Request, email: str):
    from hermes_cli.dashboard_governance.models import _norm_email

    require_governance_admin(request)
    normalized = _norm_email(email)
    async with _get_governance_policy_lock(request.app):
        raw = governance_raw_policy(request)
        users = dict(raw.get("users")) if isinstance(raw.get("users"), dict) else {}
        before = None
        found = False
        for existing_key in list(users):
            if _norm_email(str(existing_key)) == normalized:
                before = users.pop(existing_key)
                found = True
        if not found:
            raise HTTPException(
                status_code=404,
                detail={"error": "user_not_found", "message": f"unknown user: {normalized}"},
            )
        raw["users"] = users
        path = save_governance_raw_policy(raw, label="DELETE /api/governance/users/{email}")
    audit_governance_policy_change(request, action="user_delete", target=normalized, before=before, after=None)
    return {"ok": True, "path": str(path), "users": users}


@router.post("/api/governance/preview")
async def post_governance_preview(body: Dict[str, Any]):
    from hermes_cli.dashboard_governance.enforcement import safe_policy_payload, serialize_effective_access
    from hermes_cli.dashboard_governance.loader import GovernancePolicyError, parse_governance_policy
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access

    raw_policy = body.get("policy") if isinstance(body, dict) else None
    raw_subject = body.get("subject") if isinstance(body, dict) else None
    if not isinstance(raw_policy, dict):
        raise HTTPException(status_code=400, detail="policy must be an object")
    if raw_subject is not None and not isinstance(raw_subject, dict):
        raise HTTPException(status_code=400, detail="subject must be an object")
    try:
        policy = parse_governance_policy(raw_policy)
    except GovernancePolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    access = resolve_effective_access(policy, subject_from_payload(raw_subject or {}))
    return {
        "ok": True,
        "policy": safe_policy_payload(policy),
        "effective_access": serialize_effective_access(access),
    }


@router.post("/api/governance/simulate")
async def post_governance_simulate(body: Dict[str, Any]):
    from hermes_cli.dashboard_governance.loader import GovernancePolicyError, parse_governance_policy
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access
    from hermes_cli.dashboard_governance.route_catalog import route_permission

    raw_policy = body.get("policy") if isinstance(body, dict) else None
    raw_subject = body.get("subject") if isinstance(body, dict) else None
    raw_request = body.get("request") if isinstance(body, dict) else None
    if not isinstance(raw_policy, dict):
        raise HTTPException(status_code=400, detail="policy must be an object")
    if not isinstance(raw_subject, dict):
        raise HTTPException(status_code=400, detail="subject must be an object")
    if not isinstance(raw_request, dict):
        raise HTTPException(status_code=400, detail="request must be an object")
    try:
        policy = parse_governance_policy(raw_policy)
    except GovernancePolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    subject = subject_from_payload(raw_subject)
    access = resolve_effective_access(policy, subject)
    path = str(raw_request.get("path") or "")
    method = str(raw_request.get("method") or "GET").upper()
    target_profile = str(raw_request.get("profile") or "")
    required_permission = route_permission(path, method)
    allowed = True
    reason = "allowed"
    if not policy.enabled:
        reason = "governance_off"
    elif not subject.user_id and not subject.email:
        allowed = False
        reason = "unauthenticated"
    elif not access.is_route_allowed(path):
        allowed = False
        reason = "route_not_allowed"
    elif required_permission is None and path not in _SELF_ROUTES:
        allowed = False
        reason = "unknown_route"
    elif required_permission and not access.has_permission(required_permission):
        allowed = False
        reason = "permission_not_allowed"
    elif target_profile and not access.is_profile_allowed(target_profile):
        allowed = False
        reason = "profile_not_allowed"
    return {"allowed": allowed, "reason": reason, "required_permission": required_permission}


@router.get("/api/governance/audit")
async def get_governance_audit(limit: int = 100):
    from hermes_cli.dashboard_governance.audit import read_audit_events

    safe_limit = max(1, min(int(limit or 100), 500))
    return {"events": read_audit_events(limit=safe_limit)}


@router.get("/api/governance/usage")
async def get_governance_usage():
    from hermes_cli.dashboard_governance.usage import read_usage_state

    return {"usage": read_usage_state()}
