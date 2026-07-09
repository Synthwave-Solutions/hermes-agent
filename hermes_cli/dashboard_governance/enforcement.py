from __future__ import annotations

from typing import Any, Mapping, cast

from fastapi import Request
from fastapi.responses import JSONResponse

from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

from .loader import load_governance_policy
from .models import EffectiveAccess, GovernancePolicy, GovernanceSubject
from .resolver import resolve_effective_access


from .route_catalog import route_permission

_SELF_ROUTES: frozenset[str] = frozenset({
    "/api/auth/me",
    "/api/governance/me",
    "/api/governance/effective-access",
})
_PUBLIC_GOVERNANCE_BYPASS: frozenset[str] = PUBLIC_API_PATHS | frozenset({"/api/auth/providers"})


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def subject_from_request(request: Request) -> GovernanceSubject:
    """Build a governance subject from the authenticated dashboard principal.

    The auth layer owns verification. This function only translates the safe
    identity fields attached to ``request.state`` into the governance resolver's
    principal model. It deliberately does not expose or store access/refresh
    tokens.
    """

    session = getattr(request.state, "session", None)
    if session is not None and all(hasattr(session, attr) for attr in ("email", "display_name", "provider", "user_id", "org_id")):
        groups = getattr(request.state, "groups", ()) or getattr(request.state, "sso_groups", ()) or ()
        roles = getattr(request.state, "roles", ()) or ()
        claims = getattr(request.state, "claims", {}) or {}
        return GovernanceSubject(
            email=session.email,
            display_name=session.display_name,
            provider=session.provider,
            user_id=session.user_id,
            org_id=session.org_id,
            roles=tuple(_safe_str(role) for role in roles if _safe_str(role)),
            groups=tuple(_safe_str(group) for group in groups if _safe_str(group)),
            claims=claims if isinstance(claims, Mapping) else {},
        )

    token_principal = getattr(request.state, "token_principal", None)
    if token_principal is not None and all(hasattr(token_principal, attr) for attr in ("principal", "provider", "scopes")):
        principal = _safe_str(getattr(token_principal, "principal", ""))
        provider = _safe_str(getattr(token_principal, "provider", ""))
        scopes = tuple(_safe_str(scope) for scope in getattr(token_principal, "scopes", ()) if _safe_str(scope))
        return GovernanceSubject(
            email=principal if "@" in principal else "",
            display_name=principal,
            provider=provider,
            user_id=principal,
            token_scopes=scopes,
        )

    return GovernanceSubject()


def policy_for_request(request: Request) -> GovernancePolicy:
    loader = getattr(request.app.state, "governance_policy_loader", None)
    if callable(loader):
        return cast(GovernancePolicy, loader())
    return load_governance_policy()


def effective_access_for_request(request: Request) -> EffectiveAccess:
    policy = policy_for_request(request)
    return resolve_effective_access(policy, subject_from_request(request))


def serialize_effective_access(access: EffectiveAccess) -> dict[str, Any]:
    subject = access.subject
    return {
        "mode": access.mode,
        "subject": {
            "email": subject.email,
            "display_name": subject.display_name,
            "provider": subject.provider,
            "user_id": subject.user_id,
            "org_id": subject.org_id,
        },
        "roles": sorted(access.roles),
        "groups": sorted(access.groups),
        "permissions": sorted(access.permissions),
        "profiles": sorted(access.profiles),
        "routes": sorted(access.routes),
        "grant_sources": list(access.grant_sources),
        "is_admin": access.has_permission("governance:read") or access.has_permission("governance:write"),
    }



def _target_profile_from_request(request: Request) -> str:
    raw = request.query_params.get("profile", "")
    requested = _safe_str(raw)
    if requested and requested.lower() != "current":
        return requested

    path = request.url.path.rstrip("/")
    if path.startswith("/api/profiles/"):
        parts = [part for part in path.split("/") if part]
        # /api/profiles/{name}/... targets the profile named by segment 3.
        # /api/profiles/active is the sticky-active endpoint, not a profile
        # named "active".
        if len(parts) >= 3 and parts[2] != "active":
            return parts[2]
    return ""


def _audit_governance_decision(request: Request, access: EffectiveAccess, reason: str, *, report_only: bool) -> None:
    try:
        from .audit import append_audit_event

        append_audit_event(
            "would_deny" if report_only else "deny",
            subject_email=access.subject.email,
            subject_user_id=access.subject.user_id,
            path=request.url.path,
            method=request.method,
            reason=reason,
            mode=access.mode,
            report_only=report_only,
        )
    except Exception:
        # Authorization must not become unavailable because the audit sink is
        # temporarily unwritable. The denial still happens in enforce mode.
        return


def governance_decision(request: Request) -> tuple[bool, str, EffectiveAccess]:
    policy = policy_for_request(request)
    access = resolve_effective_access(policy, subject_from_request(request))
    if not policy.enabled:
        return True, "governance_off", access

    path = request.url.path
    if path in _PUBLIC_GOVERNANCE_BYPASS:
        return True, "public", access

    if not access.subject.user_id and not access.subject.email:
        return False, "unauthenticated", access

    if not access.is_route_allowed(path):
        return False, "route_not_allowed", access

    required_permission = route_permission(path, request.method)
    if required_permission is None and path not in _SELF_ROUTES:
        return False, "unknown_route", access
    if required_permission and not access.has_permission(required_permission):
        return False, "permission_not_allowed", access

    target_profile = _target_profile_from_request(request)
    if target_profile and not access.is_profile_allowed(target_profile):
        return False, "profile_not_allowed", access

    return True, "allowed", access


async def governance_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)

    try:
        allowed, reason, access = governance_decision(request)
    except Exception:
        # Policy load/parse errors must fail closed for dynamic API routes.
        raise

    request.state.governance_access = access
    request.state.governance_decision_reason = reason
    if allowed or access.mode == "report_only":
        if not allowed:
            request.state.governance_report_only_denial = reason
            _audit_governance_decision(request, access, reason, report_only=True)
        return await call_next(request)

    _audit_governance_decision(request, access, reason, report_only=False)
    if reason == "unauthenticated":
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return JSONResponse({"detail": "Forbidden"}, status_code=403)


def filter_profiles_for_access(profiles: list[dict[str, Any]], access: EffectiveAccess) -> list[dict[str, Any]]:
    if access.mode == "off" or "*" in access.profiles:
        return profiles
    return [profile for profile in profiles if access.is_profile_allowed(str(profile.get("name") or "default"))]


def safe_policy_payload(policy: GovernancePolicy) -> dict[str, Any]:
    # The policy schema is whitelist-only and should not carry secrets, but keep
    # this as a single choke point so future secret-bearing fields can be
    # redacted here before they reach the admin UI.
    return dict(policy.raw) if policy.raw else {
        "version": policy.version,
        "mode": policy.mode,
        "default_effect": policy.default_effect,
        "bootstrap_admins": list(policy.bootstrap_admins),
    }
