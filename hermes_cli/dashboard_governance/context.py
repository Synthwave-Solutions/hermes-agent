from __future__ import annotations

import json
import os
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
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
    workspace_path: str = ""
    workspace_access_check: Any = None
    # Original full policy envelopes retained by a trusted server continuation.
    # These are additional ceilings, never substitutes for current access.
    continuation_contexts: tuple[str, ...] = ()

    def cache_fingerprint(self) -> tuple:
        """Stable cache key component for schema filtering.

        Tool definitions are memoized process-wide. Enforced governance must not
        reuse another principal's filtered schema list, so include only the grant
        fields that affect tool visibility.
        """
        grants = self.access.grants
        return (
            self.access.mode,
            self.access.subject.normalized_email,
            json.dumps(_serialize_grants(self.access.deny), sort_keys=True),
            json.dumps(_serialize_grants(self.access.role_ceiling), sort_keys=True) if self.access.role_ceiling else "",
            json.dumps(_serialize_grants(self.bot_access_ceiling.grants), sort_keys=True) if self.bot_access_ceiling else "",
            tuple(sorted(grants.tools)),
            tuple(sorted(grants.toolsets)),
            tuple(sorted(grants.mcp_servers)),
            tuple(sorted((server, tuple(sorted(names))) for server, names in grants.mcp_tools.items())),
            tuple(hashlib.sha256(value.encode()).hexdigest() for value in self.continuation_contexts),
            self.workspace_path,
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
            "policy_controls_version": 1,
            "deny": _serialize_grants(access.deny),
            "role_ceiling": _serialize_grants(access.role_ceiling) if access.role_ceiling is not None else None,
            "access_level": access.access_level,
            "access_mode": access.access_mode,
            "approval_mode": access.approval_mode,
            "approval_prompt": access.approval_prompt,
            "approval_configured": access.approval_configured,
        },
        "bot_access_ceiling": _serialize_grants(ctx.bot_access_ceiling.grants) if getattr(ctx, "bot_access_ceiling", None) else None,
        "active_profile": ctx.active_profile,
        "session_id": ctx.session_id,
        "request_id": ctx.request_id,
        "approval_policy_path": getattr(ctx, "approval_policy_path", ""),
        "user_message_sha256": getattr(ctx, "user_message_sha256", ""),
        "project_workspace": getattr(ctx, "project_workspace", ""),
        "workspace_path": getattr(ctx, "workspace_path", ""),
        "continuation_contexts": list(getattr(ctx, "continuation_contexts", ())),
    }
    if getattr(ctx, "bot_access_ceiling", None) is not None:
        payload["bot_access_ceiling_context"] = serialize_context_for_env(DashboardGovernanceContext(
            subject=ctx.bot_access_ceiling.subject, access=ctx.bot_access_ceiling))
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def context_from_env_payload(payload: str) -> DashboardGovernanceContext | None:
    if not isinstance(payload, str) or len(payload) > 1024 * 1024:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    retained = data.get("continuation_contexts", [])
    if not isinstance(retained, list) or len(retained) > 16 or any(not isinstance(value, str) for value in retained):
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
        deny=_deserialize_grants(access_data.get("deny") or {}),
        role_ceiling=_deserialize_grants(access_data["role_ceiling"]) if isinstance(access_data.get("role_ceiling"), dict) else None,
        access_level=str(access_data.get("access_level") or ""),
        access_mode=str(access_data.get("access_mode") or ""),
        approval_mode=str(access_data.get("approval_mode") or "manual"),
        approval_prompt=str(access_data.get("approval_prompt") or ""),
        approval_configured=access_data.get("approval_configured") is True,
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
    if data.get("bot_access_ceiling_context") is not None:
        full_ceiling = context_from_env_payload(data["bot_access_ceiling_context"])
        if full_ceiling is None:
            return None
        ceiling = full_ceiling.access
    return DashboardGovernanceContext(
        subject=subject,
        bot_access_ceiling=ceiling,
        access=access,
        active_profile=str(data.get("active_profile") or "default"),
        session_id=str(data.get("session_id") or ""),
        request_id=str(data.get("request_id") or ""),
        approval_policy_path=str(data.get("approval_policy_path") or ""),
        user_message_sha256=str(data.get("user_message_sha256") or ""),
        project_workspace=str(data.get("project_workspace") or ""),
        workspace_path=str(data.get("workspace_path") or ""),
        continuation_contexts=tuple(retained),
    )


def policy_contexts(ctx: DashboardGovernanceContext | None) -> tuple[DashboardGovernanceContext, ...]:
    """Current envelope plus bounded, validated original continuation ceilings.

    Serialized snapshots cannot restore callbacks. Current live membership
    checks are reused only for the same principal/profile/project workspace;
    the original grants/denies remain intact and cannot add authorization.
    """
    if ctx is None:
        return ()
    result = [replace(ctx, continuation_contexts=())]
    pending = [(payload, 1) for payload in ctx.continuation_contexts]
    seen = set()
    while pending:
        payload, depth = pending.pop()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        if digest in seen:
            continue
        if depth > 8 or len(seen) >= 16:
            raise ValueError("continuation_policy_limit")
        seen.add(digest)
        original = context_from_env_payload(payload)
        if original is None:
            raise ValueError("continuation_policy_invalid")
        if (original.subject.normalized_email != ctx.subject.normalized_email
                or original.active_profile != ctx.active_profile
                or (original.subject.org_id and original.subject.org_id != ctx.subject.org_id)
                or (original.workspace_path and original.workspace_path != ctx.workspace_path)
                or (original.project_workspace and original.project_workspace != ctx.project_workspace)):
            raise ValueError("continuation_principal_mismatch")
        pending.extend((child, depth + 1) for child in original.continuation_contexts)
        if (original.access.mode == "enforce" or original.workspace_path
                or original.bot_access_ceiling is not None or original.project_workspace):
            result.append(replace(original, continuation_contexts=(),
                                  bot_access_check=ctx.bot_access_check,
                                  workspace_access_check=ctx.workspace_access_check,
                                  project_access_check=ctx.project_access_check))
    return tuple(result)


def workspace_allowed_for_context(ctx, candidate_path: str = "") -> bool:
    """Live application membership is a ceiling even with governance disabled.

    A serialized path without a trusted host callback cannot authorize a child.
    Candidate paths are checked independently: ordinary sessions may still use
    any otherwise granted root, provided that root's owner permits the actor.
    """
    root = getattr(ctx, "workspace_path", "")
    check = getattr(ctx, "workspace_access_check", None)
    if not root and check is None:
        return True
    if not callable(check):
        return False
    try:
        return all(check(path) is True for path in dict.fromkeys((root, candidate_path)) if path)
    except Exception:
        return False


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
