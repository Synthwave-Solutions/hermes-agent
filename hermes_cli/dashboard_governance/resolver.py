from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Iterable

from .models import (
    EffectiveAccess,
    GrantSet,
    GovernancePolicy,
    GovernanceSubject,
    _norm_email,
    intersect_grants,
    grant_matches,
)


def _add_permission_sources(target: dict[str, set[str]], grants: GrantSet, source: str) -> None:
    for permission in grants.permissions:
        target[permission].add(source)


def _merge_grant(current: GrantSet, incoming: GrantSet, source: str, sources: list[str], permission_sources: dict[str, set[str]]) -> GrantSet:
    sources.append(source)
    _add_permission_sources(permission_sources, incoming, source)
    return current.merge(incoming)


def _matching_sso_groups(policy: GovernancePolicy, subject_groups: Iterable[str]) -> set[str]:
    claimed = {str(group).strip() for group in subject_groups if str(group).strip()}
    if not claimed:
        return set()
    matches: set[str] = set()
    for name, group in policy.groups.items():
        if claimed & set(group.sso_groups):
            matches.add(name)
    return matches


def _role_names_for(policy: GovernancePolicy, direct_roles: Iterable[str], group_names: Iterable[str]) -> set[str]:
    names = {str(role).strip() for role in direct_roles if str(role).strip()}
    for group_name in group_names:
        group = policy.groups.get(group_name)
        if group:
            names.update(str(role).strip() for role in group.roles if str(role).strip())
    return names


def _wildcard_grants() -> GrantSet:
    star = frozenset({"*"})
    return GrantSet(
        permissions=star,
        profiles=star,
        routes=star,
        settings_read=star,
        settings_write=star,
        toolsets=star,
        tools=star,
        skills_view=star,
        skills_load=star,
        skills_manage=star,
        mcp_servers=star,
        mcp_tools={"*": star},
        model_providers=star,
        models=star,
        file_read_roots=star,
        file_write_roots=star,
        cli_commands=star,
        cli_workdir_roots=star,
        workspaces=star,
    )


def _level_ceiling(grants: GrantSet, level: str) -> GrantSet:
    """Levels constrain configured role scopes; they do not invent resources."""
    if level == "admin":
        return grants
    from .route_catalog import ROUTE_CATALOG
    known = {p for rule in ROUTE_CATALOG for p in (rule.read_permission, rule.write_permission) if p}
    known.update({"self:read", "config:write", "terminal:use"})
    def permitted(p):
        if p.startswith("governance:"):
            return False
        if level == "user" and (p.endswith(":admin") or p in {
            "terminal:use", "config:write", "system:write", "system:ops", "gateway:restart",
            "model:write", "mcp:write", "plugins:write",
        }):
            return False
        return True
    return replace(grants, permissions=frozenset(
        p for p in known | set(grants.permissions)
        if not any(char in p for char in "*?[") and permitted(p) and grant_matches(grants.permissions, p)))


def resolve_effective_access(policy: GovernancePolicy, subject: GovernanceSubject) -> EffectiveAccess:
    email = _norm_email(subject.email)
    sources: list[str] = []
    permission_sources: dict[str, set[str]] = defaultdict(set)
    grants = GrantSet()
    roles: set[str] = set(str(role).strip() for role in subject.roles if str(role).strip())
    groups: set[str] = set(str(group).strip() for group in subject.groups if str(group).strip())

    if email and email in policy.bootstrap_admins:
        grants = _merge_grant(grants, _wildcard_grants(), "bootstrap_admin", sources, permission_sources)
        roles.add("owner")

    user = policy.users.get(email) if email else None
    if user:
        roles.update(user.roles)
        groups.update(user.groups)

    groups.update(_matching_sso_groups(policy, subject.groups))
    roles.update(_role_names_for(policy, roles, groups))

    # Apply role grants before group/user grants so direct grants can be shown last in previews.
    for role_name in sorted(roles):
        role = policy.roles.get(role_name)
        if role:
            grants = _merge_grant(grants, role.grants, f"role:{role_name}", sources, permission_sources)
    for group_name in sorted(groups):
        group = policy.groups.get(group_name)
        if group:
            grants = _merge_grant(grants, group.grants, f"group:{group_name}", sources, permission_sources)
    role_ceiling = None
    if user and (user.access_mode or user.access_level) and email not in policy.bootstrap_admins:
        level = user.access_level or "user"
        # The assignable admin preset is powerful, but unlike bootstrap
        # ownership remains subject to explicit per-user denies.
        if level == "admin":
            grants = grants.merge(_wildcard_grants())
        if not user.access_mode:
            grants = grants.merge(user.grants)
        role_ceiling = _level_ceiling(grants, level)
        grants = intersect_grants(user.grants, role_ceiling) if user.access_mode == "whitelist" else role_ceiling
        sources.append(f"access_mode:{user.access_mode or 'legacy'}")
        roles.difference_update({"owner", "admin"})
    if user:
        if role_ceiling is None:
            grants = _merge_grant(grants, user.grants, f"user:{email}", sources, permission_sources)
        else:
            sources.append(f"user:{email}")
    if user and not user.deny.is_empty() and email not in policy.bootstrap_admins:
        # Per-user off-toggles subtract AFTER the full union so they win from
        # any role/group grant. Bootstrap admins are exempt (never-deny
        # principals: a stray deny: "*" must not brick the owner).
        grants = grants.subtract(user.deny)
        sources.append(f"deny:user:{email}")
        for permission in user.deny.permissions:
            permission_sources.pop(permission, None)

    if role_ceiling is not None and user.access_level == "admin" and grant_matches(grants.permissions, "governance:write") and not grant_matches(user.deny.permissions, "governance:write"):
        roles.add("admin")
    return EffectiveAccess(
        subject=subject,
        mode=policy.mode,
        roles=frozenset(roles),
        groups=frozenset(groups),
        permissions=grants.permissions,
        profiles=grants.profiles,
        routes=grants.routes,
        grants=grants,
        grant_sources=tuple(sources),
        permission_sources={key: tuple(sorted(value)) for key, value in permission_sources.items()},
        deny=user.deny if user and email not in policy.bootstrap_admins else GrantSet(),
        role_ceiling=role_ceiling,
        access_level=user.access_level if user else "",
        access_mode=user.access_mode if user else "",
        approval_mode=user.approval_mode if user else "manual",
        approval_prompt=user.approval_prompt if user else "",
        approval_configured=user.approval_configured if user else False,
    )
