from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterator

from .models import EffectiveAccess, GovernanceSubject, GrantSet

GOVERNANCE_CONTEXT_ENV = "HERMES_DASHBOARD_GOVERNANCE_CONTEXT"


@dataclass(frozen=True)
class DashboardGovernanceContext:
    """Runtime governance principal bound to a dashboard-started agent run."""

    subject: GovernanceSubject
    access: EffectiveAccess
    active_profile: str = "default"
    session_id: str = ""
    request_id: str = ""
    user_message_sha256: str = ""
    user_message_redacted: str = ""
    bot_access_ceiling: EffectiveAccess | None = None
    bot_access_check: Any = None
    approval_waiter: Any = None
    approval_policy_path: str = ""
    project_workspace: str = ""
    project_access_check: Any = None

    def cache_fingerprint(self) -> tuple:
        """Stable cache key component for schema filtering.

        Tool definitions are memoized process-wide. Enforced governance must not
        reuse another principal's filtered schema list, so include only the grant
        fields that affect tool visibility.
        """
        grants = self.access.grants
        return (
            self.access.mode,
            json.dumps(_serialize_grants(self.bot_access_ceiling.grants), sort_keys=True) if self.bot_access_ceiling else "",
            tuple(sorted(grants.tools)),
            tuple(sorted(grants.toolsets)),
            tuple(sorted(grants.mcp_servers)),
            tuple(sorted((server, tuple(sorted(names))) for server, names in grants.mcp_tools.items())),
        )


def _list(values: frozenset[str] | tuple[str, ...]) -> list[str]:
    return sorted(str(value) for value in values if str(value))


def _set(values: Any) -> frozenset[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(value) for value in values if str(value))


def _serialize_grants(grants: GrantSet) -> dict[str, Any]:
    return {
        "permissions": _list(grants.permissions),
        "profiles": _list(grants.profiles),
        "routes": _list(grants.routes),
        "settings_read": _list(grants.settings_read),
        "settings_write": _list(grants.settings_write),
        "toolsets": _list(grants.toolsets),
        "tools": _list(grants.tools),
        "skills_view": _list(grants.skills_view),
        "skills_load": _list(grants.skills_load),
        "skills_manage": _list(grants.skills_manage),
        "mcp_servers": _list(grants.mcp_servers),
        "mcp_tools": {str(server): _list(names) for server, names in grants.mcp_tools.items()},
        "model_providers": _list(grants.model_providers),
        "models": _list(grants.models),
        "file_read_roots": _list(grants.file_read_roots),
        "file_write_roots": _list(grants.file_write_roots),
        "file_denied_globs": _list(grants.file_denied_globs),
        "file_allow_globs": _list(grants.file_allow_globs),
        "cli_commands": _list(grants.cli_commands),
        "cli_approval_commands": _list(grants.cli_approval_commands),
        "cli_denied_commands": _list(grants.cli_denied_commands),
        "cli_workdir_roots": _list(grants.cli_workdir_roots),
        "env_vars": _list(grants.env_vars),
        "workspaces": _list(grants.workspaces),
        "usage_caps": dict(grants.usage_caps),
    }


def _deserialize_grants(data: dict[str, Any]) -> GrantSet:
    raw_mcp_tools = data.get("mcp_tools") if isinstance(data, dict) else {}
    mcp_tools = {
        str(server): _set(names)
        for server, names in (raw_mcp_tools.items() if isinstance(raw_mcp_tools, dict) else [])
    }
    return GrantSet(
        permissions=_set(data.get("permissions")),
        profiles=_set(data.get("profiles")),
        routes=_set(data.get("routes")),
        settings_read=_set(data.get("settings_read")),
        settings_write=_set(data.get("settings_write")),
        toolsets=_set(data.get("toolsets")),
        tools=_set(data.get("tools")),
        skills_view=_set(data.get("skills_view")),
        skills_load=_set(data.get("skills_load")),
        skills_manage=_set(data.get("skills_manage")),
        mcp_servers=_set(data.get("mcp_servers")),
        mcp_tools=mcp_tools,
        model_providers=_set(data.get("model_providers")),
        models=_set(data.get("models")),
        file_read_roots=_set(data.get("file_read_roots")),
        file_write_roots=_set(data.get("file_write_roots")),
        file_denied_globs=_set(data.get("file_denied_globs")),
        file_allow_globs=_set(data.get("file_allow_globs")),
        cli_commands=_set(data.get("cli_commands")),
        cli_approval_commands=_set(data.get("cli_approval_commands")),
        cli_denied_commands=_set(data.get("cli_denied_commands")),
        cli_workdir_roots=_set(data.get("cli_workdir_roots")),
        env_vars=_set(data.get("env_vars")),
        workspaces=_set(data.get("workspaces")),
        usage_caps=dict(data.get("usage_caps") or {}),
    )


def serialize_context_for_env(ctx: DashboardGovernanceContext) -> str:
    access = ctx.access
    subject = access.subject
    payload = {
        "subject": {
            "email": subject.email,
            "display_name": subject.display_name,
            "provider": subject.provider,
            "user_id": subject.user_id,
            "org_id": subject.org_id,
            "groups": _list(subject.groups),
            "roles": _list(subject.roles),
            "token_scopes": _list(subject.token_scopes),
        },
        "access": {
            "mode": access.mode,
            "roles": _list(access.roles),
            "groups": _list(access.groups),
            "permissions": _list(access.permissions),
            "profiles": _list(access.profiles),
            "routes": _list(access.routes),
            "grant_sources": list(access.grant_sources),
            "grants": _serialize_grants(access.grants),
        },
        "bot_access_ceiling": _serialize_grants(ctx.bot_access_ceiling.grants) if getattr(ctx, "bot_access_ceiling", None) else None,
        "active_profile": ctx.active_profile,
        "session_id": ctx.session_id,
        "request_id": ctx.request_id,
        "user_message_sha256": getattr(ctx, "user_message_sha256", ""),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def context_from_env_payload(payload: str) -> DashboardGovernanceContext | None:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    subject_raw = data.get("subject")
    access_raw = data.get("access")
    subject_data: dict[str, Any] = subject_raw if isinstance(subject_raw, dict) else {}
    access_data: dict[str, Any] = access_raw if isinstance(access_raw, dict) else {}
    subject = GovernanceSubject(
        email=str(subject_data.get("email") or ""),
        display_name=str(subject_data.get("display_name") or ""),
        provider=str(subject_data.get("provider") or ""),
        user_id=str(subject_data.get("user_id") or ""),
        org_id=str(subject_data.get("org_id") or ""),
        groups=tuple(sorted(_set(subject_data.get("groups")))),
        roles=tuple(sorted(_set(subject_data.get("roles")))),
        token_scopes=tuple(sorted(_set(subject_data.get("token_scopes")))),
    )
    grants_raw = access_data.get("grants")
    access = EffectiveAccess(
        subject=subject,
        mode=str(access_data.get("mode") or "off"),
        roles=_set(access_data.get("roles")),
        groups=_set(access_data.get("groups")),
        permissions=_set(access_data.get("permissions")),
        profiles=_set(access_data.get("profiles")),
        routes=_set(access_data.get("routes")),
        grants=_deserialize_grants(grants_raw if isinstance(grants_raw, dict) else {}),
        grant_sources=tuple(str(item) for item in (access_data.get("grant_sources") or ()) if str(item)),
    )
    ceiling_raw = data.get("bot_access_ceiling")
    if ceiling_raw is not None and not isinstance(ceiling_raw, dict):
        return None
    ceiling = None
    if ceiling_raw is not None:
        ceiling = EffectiveAccess(subject=subject, mode="enforce",
                                  roles=access.roles, groups=access.groups, grant_sources=access.grant_sources,
                                  permissions=frozenset({"*"}),
                                  profiles=frozenset({str(data.get("active_profile") or "default")}),
                                  grants=_deserialize_grants(ceiling_raw))
    return DashboardGovernanceContext(
        subject=subject,
        bot_access_ceiling=ceiling,
        access=access,
        active_profile=str(data.get("active_profile") or "default"),
        session_id=str(data.get("session_id") or ""),
        request_id=str(data.get("request_id") or ""),
        user_message_sha256=str(data.get("user_message_sha256") or ""),
    )


def _context_from_env() -> DashboardGovernanceContext | None:
    payload = os.environ.get(GOVERNANCE_CONTEXT_ENV, "")
    if not payload:
        return None
    return context_from_env_payload(payload)


_current_governance_context: ContextVar[DashboardGovernanceContext | None] = ContextVar(
    "dashboard_governance_context",
    default=None,
)


def current_governance_context() -> DashboardGovernanceContext | None:
    return _current_governance_context.get() or _context_from_env()


def bind_governance_context(ctx: DashboardGovernanceContext | None) -> Token:
    return _current_governance_context.set(ctx)


def reset_governance_context(token: Token) -> None:
    _current_governance_context.reset(token)


@contextmanager
def governance_context(ctx: DashboardGovernanceContext | None) -> Iterator[None]:
    token = bind_governance_context(ctx)
    try:
        yield
    finally:
        reset_governance_context(token)
