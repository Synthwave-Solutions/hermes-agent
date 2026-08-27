from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import (
    EffectiveAccess,
    GrantSet,
    GovernancePolicy,
    GovernanceSubject,
    _norm_email,
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
    if user:
        grants = _merge_grant(grants, user.grants, f"user:{email}", sources, permission_sources)

    # Deny overrides grants, applied LAST so a per-user (or per-group) deny
    # beats anything a role or group handed out. Bootstrap admins are
    # never-deny: their wildcard access is not subtractable (a deny on an owner
    # is refused at write time and ignored here as defence in depth).
    # Mirrors the hermes-webui vendored resolver (api/governance/resolver.py):
    # only the per-USER deny is subtracted, so a person is individually blocked
    # from a capability a role or group otherwise grants. Kept identical across
    # the two copies on purpose: a deny that applied in one layer but not the
    # other would be a silent hole.
    ud = getattr(user, "deny", None) if user else None
    if user and ud is not None and not ud.is_empty() and email not in policy.bootstrap_admins:
        grants = grants.subtract(ud)
        sources.append(f"deny:user:{email}")
        # A subtracted permission must also leave the source map, or a preview
        # would still name a source for a permission the subject no longer holds.
        for perm in list(permission_sources):
            if perm not in grants.permissions:
                permission_sources.pop(perm, None)

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
    )
