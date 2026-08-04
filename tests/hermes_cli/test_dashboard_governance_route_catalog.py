from __future__ import annotations

from hermes_cli.dashboard_governance.route_catalog import route_permission
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS


def test_route_catalog_classifies_all_non_public_fastapi_api_routes():
    from hermes_cli import web_server

    missing: list[tuple[str, str]] = []
    for route in web_server.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            if route_permission(path, method) is None and path not in {
                "/api/auth/me",
                "/api/governance/me",
                "/api/governance/effective-access",
            }:
                missing.append((method, path))

    assert missing == []


def test_management_route_catalog_maps_core_endpoint_families():
    cases = [
        ("/api/config", "GET", "config:read"),
        ("/api/config", "PUT", "config:write"),
        ("/api/env", "GET", "env:read"),
        ("/api/env", "POST", "env:write"),
        ("/api/model/options", "GET", "model:read"),
        ("/api/model/set", "POST", "model:write"),
        ("/api/providers/custom-endpoints", "GET", "config:read"),
        ("/api/providers/custom-endpoints/abc/activate", "POST", "config:write"),
        ("/api/skills", "GET", "skills:read"),
        ("/api/skills/install", "POST", "skills:write"),
        ("/api/tools/toolsets", "GET", "tools:read"),
        ("/api/tools/toolsets", "PUT", "tools:write"),
        ("/api/tools/terminal/backends", "GET", "tools:read"),
        ("/api/tools/terminal/backend", "PUT", "tools:write"),
        ("/api/mcp/servers", "GET", "mcp:read"),
        ("/api/mcp/reload", "POST", "mcp:write"),
        ("/api/cron/jobs", "GET", "cron:read"),
        ("/api/cron/jobs", "POST", "cron:write"),
        ("/api/cron/jobs/abc/run", "POST", "cron:run"),
        ("/api/system/stats", "GET", "system:read"),
        ("/api/ssh/ownership", "GET", "system:read"),
        ("/api/egress/status", "GET", "system:read"),
        ("/api/hermes/update", "POST", "system:ops"),
        ("/api/gateway/restart", "POST", "gateway:restart"),
        ("/api/webhooks", "GET", "webhooks:read"),
        ("/api/webhooks/foo", "DELETE", "webhooks:write"),
        ("/api/channels", "GET", "channels:read"),
        ("/api/channels/test", "POST", "channels:write"),
        ("/api/pairing/approve", "POST", "pairing:admin"),
        ("/api/profiles/sessions", "GET", "sessions:read"),
        ("/api/git/review/push", "POST", "git:write"),
        ("/api/git/status", "GET", "git:read"),
        ("/api/audio/speak", "POST", "audio:write"),
        ("/api/audio/elevenlabs/voices", "GET", "audio:read"),
        ("/api/memory/providers/foo/config", "PUT", "memory:write"),
        ("/api/memory/providers/foo/config", "GET", "memory:read"),
        ("/api/curator/run", "POST", "curator:write"),
        ("/api/curator", "GET", "curator:read"),
        ("/api/learning/node", "DELETE", "learning:write"),
        ("/api/learning/graph", "GET", "learning:read"),
    ]

    for path, method, expected in cases:
        assert route_permission(path, method) == expected, (path, method)


def test_route_catalog_self_routes_do_not_need_permissions():
    assert route_permission("/api/auth/me", "GET") is None
    assert route_permission("/api/governance/me", "GET") is None
    assert route_permission("/api/governance/effective-access", "GET") is None


def test_route_catalog_prefixes_require_segment_boundary():
    assert route_permission("/api/configuration", "GET") is None
    assert route_permission("/api/modeling", "GET") is None
    assert route_permission("/api/governancex", "GET") is None


def test_route_catalog_fails_closed_for_unknown_api_route():
    assert route_permission("/api/new-unclassified-management-route", "GET") is None
