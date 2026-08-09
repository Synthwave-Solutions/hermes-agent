from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


_ALLOWED_MODES = frozenset({"off", "report_only", "enforce"})


def _norm_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _string_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value.strip()}) if value.strip() else frozenset()
    if isinstance(value, Mapping):
        return frozenset(str(k).strip() for k in value.keys() if str(k).strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(item).strip() for item in value if str(item).strip())
    return frozenset({str(value).strip()}) if str(value).strip() else frozenset()


def _deep_merge_grants(base: "GrantSet", other: "GrantSet") -> "GrantSet":
    return GrantSet(
        permissions=base.permissions | other.permissions,
        profiles=base.profiles | other.profiles,
        routes=base.routes | other.routes,
        settings_read=base.settings_read | other.settings_read,
        settings_write=base.settings_write | other.settings_write,
        toolsets=base.toolsets | other.toolsets,
        tools=base.tools | other.tools,
        skills_view=base.skills_view | other.skills_view,
        skills_load=base.skills_load | other.skills_load,
        skills_manage=base.skills_manage | other.skills_manage,
        mcp_servers=base.mcp_servers | other.mcp_servers,
        mcp_tools={**base.mcp_tools, **{k: base.mcp_tools.get(k, frozenset()) | v for k, v in other.mcp_tools.items()}},
        model_providers=base.model_providers | other.model_providers,
        models=base.models | other.models,
        file_read_roots=base.file_read_roots | other.file_read_roots,
        file_write_roots=base.file_write_roots | other.file_write_roots,
        file_denied_globs=base.file_denied_globs | other.file_denied_globs,
        cli_commands=base.cli_commands | other.cli_commands,
        cli_workdir_roots=base.cli_workdir_roots | other.cli_workdir_roots,
        workspaces=base.workspaces | other.workspaces,
        usage_caps={**base.usage_caps, **other.usage_caps},
    )


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    sources: tuple[str, ...] = ()
    report_only: bool = False


@dataclass(frozen=True)
class GrantSet:
    permissions: frozenset[str] = field(default_factory=frozenset)
    profiles: frozenset[str] = field(default_factory=frozenset)
    routes: frozenset[str] = field(default_factory=frozenset)
    settings_read: frozenset[str] = field(default_factory=frozenset)
    settings_write: frozenset[str] = field(default_factory=frozenset)
    toolsets: frozenset[str] = field(default_factory=frozenset)
    tools: frozenset[str] = field(default_factory=frozenset)
    skills_view: frozenset[str] = field(default_factory=frozenset)
    skills_load: frozenset[str] = field(default_factory=frozenset)
    skills_manage: frozenset[str] = field(default_factory=frozenset)
    mcp_servers: frozenset[str] = field(default_factory=frozenset)
    mcp_tools: dict[str, frozenset[str]] = field(default_factory=dict)
    model_providers: frozenset[str] = field(default_factory=frozenset)
    models: frozenset[str] = field(default_factory=frozenset)
    file_read_roots: frozenset[str] = field(default_factory=frozenset)
    file_write_roots: frozenset[str] = field(default_factory=frozenset)
    file_denied_globs: frozenset[str] = field(default_factory=frozenset)
    cli_commands: frozenset[str] = field(default_factory=frozenset)
    cli_workdir_roots: frozenset[str] = field(default_factory=frozenset)
    # Workspaces zijn de mappen die de WebUI in zijn kiezer toont. Ze horen
    # hier thuis en niet in een los bestand: anders bepaalt de ene plek wat je
    # ziet en de andere wat je mag openen, en dan lopen die uit elkaar.
    workspaces: frozenset[str] = field(default_factory=frozenset)
    usage_caps: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "GrantSet":
        if not isinstance(data, Mapping):
            return cls()
        settings = data.get("settings") if isinstance(data.get("settings"), Mapping) else {}
        tools = data.get("tools") if isinstance(data.get("tools"), Mapping) else {}
        skills = data.get("skills") if isinstance(data.get("skills"), Mapping) else {}
        mcp = data.get("mcp") if isinstance(data.get("mcp"), Mapping) else {}
        models = data.get("models") if isinstance(data.get("models"), Mapping) else {}
        files = data.get("files") if isinstance(data.get("files"), Mapping) else {}
        cli = data.get("cli") if isinstance(data.get("cli"), Mapping) else {}
        mcp_tools: dict[str, frozenset[str]] = {}
        raw_mcp_tools = mcp.get("tools") if isinstance(mcp, Mapping) else None
        if isinstance(raw_mcp_tools, Mapping):
            for server, names in raw_mcp_tools.items():
                mcp_tools[str(server)] = _string_set(names)
        command_ids: set[str] = set()
        for item in cli.get("commands", []) if isinstance(cli, Mapping) else []:
            if isinstance(item, Mapping):
                ident = item.get("id") or item.get("argv0")
                if ident:
                    command_ids.add(str(ident))
            elif item:
                command_ids.add(str(item))
        return cls(
            permissions=_string_set(data.get("permissions")),
            profiles=_string_set(data.get("profiles")),
            routes=_string_set(data.get("routes")),
            settings_read=_string_set(settings.get("read") if isinstance(settings, Mapping) else None),
            settings_write=_string_set(settings.get("write") if isinstance(settings, Mapping) else None),
            toolsets=_string_set(tools.get("toolsets") if isinstance(tools, Mapping) else data.get("toolsets")),
            tools=_string_set(tools.get("builtins") if isinstance(tools, Mapping) else data.get("tools")),
            skills_view=_string_set(skills.get("view") if isinstance(skills, Mapping) else None),
            skills_load=_string_set(skills.get("load") if isinstance(skills, Mapping) else None),
            skills_manage=_string_set(skills.get("manage") if isinstance(skills, Mapping) else None),
            mcp_servers=_string_set(mcp.get("servers") if isinstance(mcp, Mapping) else None),
            mcp_tools=mcp_tools,
            model_providers=_string_set(models.get("providers") if isinstance(models, Mapping) else None),
            models=_string_set(models.get("models") if isinstance(models, Mapping) else None),
            file_read_roots=_string_set(files.get("read_roots") if isinstance(files, Mapping) else None),
            file_write_roots=_string_set(files.get("write_roots") if isinstance(files, Mapping) else None),
            file_denied_globs=_string_set(files.get("denied_globs") if isinstance(files, Mapping) else None),
            cli_commands=frozenset(command_ids),
            cli_workdir_roots=_string_set(cli.get("workdir_roots") if isinstance(cli, Mapping) else None),
            workspaces=_string_set(data.get("workspaces")),
            usage_caps=dict(data.get("usage_caps") or {}),
        )

    def merge(self, other: "GrantSet") -> "GrantSet":
        return _deep_merge_grants(self, other)


@dataclass(frozen=True)
class GovernanceRole:
    name: str
    grants: GrantSet = field(default_factory=GrantSet)
    description: str = ""


@dataclass(frozen=True)
class GovernanceGroup:
    name: str
    roles: tuple[str, ...] = ()
    grants: GrantSet = field(default_factory=GrantSet)
    sso_groups: frozenset[str] = field(default_factory=frozenset)
    description: str = ""


@dataclass(frozen=True)
class GovernanceUser:
    email: str
    roles: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    grants: GrantSet = field(default_factory=GrantSet)


@dataclass(frozen=True)
class GovernancePolicy:
    version: int = 1
    mode: str = "off"
    default_effect: str = "deny"
    bootstrap_admins: tuple[str, ...] = ()
    roles: dict[str, GovernanceRole] = field(default_factory=dict)
    groups: dict[str, GovernanceGroup] = field(default_factory=dict)
    users: dict[str, GovernanceUser] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.mode in {"report_only", "enforce"}

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"


@dataclass(frozen=True)
class GovernanceSubject:
    email: str = ""
    display_name: str = ""
    provider: str = ""
    user_id: str = ""
    org_id: str = ""
    roles: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    claims: Mapping[str, Any] = field(default_factory=dict)
    token_scopes: tuple[str, ...] = ()

    @property
    def normalized_email(self) -> str:
        return _norm_email(self.email)


@dataclass(frozen=True)
class EffectiveAccess:
    subject: GovernanceSubject
    mode: str
    roles: frozenset[str] = field(default_factory=frozenset)
    groups: frozenset[str] = field(default_factory=frozenset)
    permissions: frozenset[str] = field(default_factory=frozenset)
    profiles: frozenset[str] = field(default_factory=frozenset)
    routes: frozenset[str] = field(default_factory=frozenset)
    grants: GrantSet = field(default_factory=GrantSet)
    grant_sources: tuple[str, ...] = ()
    permission_sources: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def _allowed_by_set(self, values: frozenset[str], value: str) -> bool:
        return "*" in values or value in values

    def has_permission(self, permission: str) -> bool:
        return self._allowed_by_set(self.permissions, permission)

    def is_profile_allowed(self, profile: str) -> bool:
        return self._allowed_by_set(self.profiles, profile or "default")

    def is_route_allowed(self, path: str) -> bool:
        if "*" in self.routes:
            return True
        return any(path == route or (route.endswith("*") and path.startswith(route[:-1])) for route in self.routes)

    def is_tool_allowed(self, tool_name: str) -> bool:
        return self._allowed_by_set(self.grants.tools, tool_name)

    def explain_permission(self, permission: str) -> AccessDecision:
        if self.has_permission(permission):
            return AccessDecision(True, "allowed", tuple(self.permission_sources.get(permission) or self.permission_sources.get("*") or ()))
        return AccessDecision(False, "not_whitelisted", ())
