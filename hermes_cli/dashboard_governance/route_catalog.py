from __future__ import annotations

from dataclasses import dataclass

_MUTATION_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SELF_ROUTES: frozenset[str] = frozenset({
    "/api/auth/me",
    "/api/governance/me",
    "/api/governance/effective-access",
})


@dataclass(frozen=True)
class RouteRule:
    pattern: str
    read_permission: str | None
    write_permission: str | None = None
    match: str = "prefix"

    def matches(self, path: str) -> bool:
        if self.match == "exact":
            return path == self.pattern
        return path == self.pattern or path.startswith(self.pattern.rstrip("/") + "/")

    def permission_for(self, method: str) -> str | None:
        if method.upper() in _MUTATION_METHODS:
            return self.write_permission or self.read_permission
        return self.read_permission


# Ordered most-specific first. Unknown /api/* routes deliberately return None;
# enforcement treats that as unknown_route so new endpoints fail closed until
# classified here.
ROUTE_CATALOG: tuple[RouteRule, ...] = (
    RouteRule("/api/governance/policy", "governance:read", "governance:write"),
    RouteRule("/api/governance/preview", "governance:preview"),
    RouteRule("/api/governance/simulate", "governance:preview", "governance:preview"),
    RouteRule("/api/governance/audit", "governance:audit:read"),
    RouteRule("/api/governance/usage", "governance:usage:read"),
    RouteRule("/api/governance/users", "governance:read", "governance:write"),
    RouteRule("/api/governance/groups", "governance:read", "governance:write"),
    RouteRule("/api/governance", "governance:read", "governance:write"),
    RouteRule("/api/profiles/active", "profiles:read", "profiles:admin", match="exact"),
    RouteRule("/api/profiles/sessions", "sessions:read", match="exact"),
    RouteRule("/api/profiles", "profiles:read", "profiles:admin"),
    RouteRule("/api/sessions", "sessions:read", "sessions:write"),
    RouteRule("/api/chat", "chat:use", "chat:use"),
    RouteRule("/api/pty", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/ws", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/pub", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/events", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/files", "files:read", "files:write"),
    RouteRule("/api/fs/write-text", "files:write", "files:write", match="exact"),
    RouteRule("/api/fs", "files:read", "files:write"),
    RouteRule("/api/git/review/stage", "git:write", "git:write", match="exact"),
    RouteRule("/api/git/review/unstage", "git:write", "git:write", match="exact"),
    RouteRule("/api/git/review/revert", "git:write", "git:write", match="exact"),
    RouteRule("/api/git/review/commit", "git:write", "git:write", match="exact"),
    RouteRule("/api/git/review/push", "git:write", "git:write", match="exact"),
    RouteRule("/api/git/review/create-pr", "git:write", "git:write", match="exact"),
    RouteRule("/api/git/worktree/add", "git:write", "git:write", match="exact"),
    RouteRule("/api/git/worktree/remove", "git:write", "git:write", match="exact"),
    RouteRule("/api/git/branch/switch", "git:write", "git:write", match="exact"),
    RouteRule("/api/git", "git:read", "git:write"),
    RouteRule("/api/logs", "logs:read"),
    RouteRule("/api/analytics", "analytics:read"),
    RouteRule("/api/config/schema", "config:read", match="exact"),
    RouteRule("/api/config/defaults", "config:read", match="exact"),
    RouteRule("/api/config", "config:read", "config:write"),
    RouteRule("/api/env", "env:read", "env:write"),
    RouteRule("/api/providers/validate", "config:write", "config:write", match="exact"),
    RouteRule("/api/providers/oauth", "config:read", "config:write"),
    RouteRule("/api/providers/custom-endpoints", "config:read", "config:write"),
    RouteRule("/api/credentials/pool", "config:read", "config:write"),
    RouteRule("/api/auth/providers", "status:read", match="exact"),
    RouteRule("/api/auth/ws-ticket", "chat:use", "chat:use", match="exact"),
    RouteRule("/api/model/set", "model:write", "model:write"),
    RouteRule("/api/model/auxiliary", "model:read", "model:write"),
    RouteRule("/api/model", "model:read", "model:write"),
    RouteRule("/api/skills", "skills:read", "skills:write"),
    RouteRule("/api/tools/toolsets", "tools:read", "tools:write"),
    RouteRule("/api/tools/computer-use", "tools:read", "tools:write"),
    RouteRule("/api/tools/terminal/backends", "tools:read", match="exact"),
    RouteRule("/api/tools/terminal/backend", "tools:read", "tools:write", match="exact"),
    RouteRule("/api/mcp", "mcp:read", "mcp:write"),
    RouteRule("/api/plugins", "plugins:read", "plugins:write"),
    RouteRule("/api/dashboard/agent-plugins", "plugins:read", "plugins:write"),
    RouteRule("/api/dashboard/plugins", "plugins:read", "plugins:write"),
    RouteRule("/api/dashboard/plugin-providers", "plugins:read", "plugins:write"),
    RouteRule("/api/dashboard/theme", "dashboard:read", "dashboard:write", match="exact"),
    RouteRule("/api/dashboard/themes", "dashboard:read", match="exact"),
    RouteRule("/api/dashboard/font", "dashboard:read", "dashboard:write", match="exact"),
    RouteRule("/api/cron", "cron:read", "cron:write"),
    RouteRule("/api/webhooks", "webhooks:read", "webhooks:write"),
    RouteRule("/api/messaging", "channels:read", "channels:write"),
    RouteRule("/api/channels", "channels:read", "channels:write"),
    RouteRule("/api/pairing", "pairing:admin", "pairing:admin"),
    RouteRule("/api/gateway/restart", "gateway:restart", "gateway:restart", match="exact"),
    RouteRule("/api/gateway/drain", "gateway:restart", "gateway:restart", match="exact"),
    RouteRule("/api/gateway", "gateway:read", "gateway:restart"),
    RouteRule("/api/system", "system:read", "system:ops"),
    RouteRule("/api/ssh/ownership", "system:read", match="exact"),
    RouteRule("/api/egress/status", "system:read", match="exact"),
    RouteRule("/api/hermes/update/check", "system:read", match="exact"),
    RouteRule("/api/hermes/update", "system:ops", "system:ops", match="exact"),
    RouteRule("/api/ops", "system:ops", "system:ops"),
    RouteRule("/api/actions", "system:read"),
    RouteRule("/api/curator", "curator:read", "curator:write"),
    RouteRule("/api/learning", "learning:read", "learning:write"),
    RouteRule("/api/portal", "status:read"),
    RouteRule("/api/audio/elevenlabs/voices", "audio:read", match="exact"),
    RouteRule("/api/audio", "audio:read", "audio:write"),
    RouteRule("/api/media", "files:read", match="exact"),
    RouteRule("/api/memory", "memory:read", "memory:write"),
    RouteRule("/api/status", "status:read", match="exact"),
)


def route_permission(path: str, method: str) -> str | None:
    if path in _SELF_ROUTES:
        return None
    for rule in ROUTE_CATALOG:
        if rule.matches(path):
            if method.upper() == "POST" and path.startswith("/api/cron") and path.rstrip("/").endswith("/run"):
                return "cron:run"
            return rule.permission_for(method)
    return None
