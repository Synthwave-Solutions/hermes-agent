"""App connections: use the signed-in person's own Nango connections from a chat turn.

The host (SynthPulse WebUI) binds a resolver per turn with ``bind_app_connections``.
The resolver owns every credential and every ownership decision: it only exposes the
caller's own connections (plus organisation connections for admins), proxies REST calls
through Nango and calls MCP connectors with a token the model never sees. Without a bound
resolver the tool refuses (fail closed): a cron job, a routed profile or a bot must never
fall back to the installation owner's accounts.

Reads run directly. Anything that can change data at the provider (non-GET REST calls,
GraphQL mutations, MCP tools without ``readOnlyHint``) goes through the human approval gate.
"""
from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any, Protocol

from tools.registry import registry, tool_error


class AppConnectionsResolver(Protocol):
    def list(self) -> list[dict]: ...
    def request(self, integration: str, connection_id: str | None, method: str, path: str,
                query: dict | None, body: Any, headers: dict | None) -> dict: ...
    def mcp(self, integration: str, connection_id: str | None, method: str,
            name: str | None, arguments: dict | None) -> dict: ...
    def mcp_tool_is_read_only(self, integration: str, connection_id: str | None, name: str) -> bool: ...


_resolver: ContextVar[AppConnectionsResolver | None] = ContextVar("app_connections_resolver", default=None)


def bind_app_connections(resolver: AppConnectionsResolver):
    """Bind the host's per-identity resolver for the current turn; returns a reset token."""
    return _resolver.set(resolver)


def reset_app_connections(token) -> None:
    _resolver.reset(token)


def app_connections_bound() -> bool:
    return _resolver.get() is not None


def personal_cli_env() -> dict[str, str]:
    """Environment variables from the bound person's own API-key connections (empty when
    unbound or when the host resolver has no CLI mapping). Never raises: a terminal must
    still start when Nango is down."""
    resolver = _resolver.get()
    getter = getattr(resolver, "cli_env", None) if resolver is not None else None
    if getter is None:
        return {}
    try:
        env = getter()
    except Exception:
        return {}
    return {str(k): str(v) for k, v in (env or {}).items() if isinstance(k, str) and isinstance(v, str)}


_READ_METHODS = {"GET", "HEAD", "OPTIONS"}
_ALL_METHODS = _READ_METHODS | {"POST", "PUT", "PATCH", "DELETE"}
_MUTATION_RE = re.compile(r"^\s*(#[^\n]*\n\s*)*mutation\b", re.IGNORECASE)


def _graphql_read(path: str, body: Any) -> bool:
    """A POST to a GraphQL endpoint whose document is a query (no mutation) is a read."""
    if not re.search(r"graphql/?$", path.split("?", 1)[0], re.IGNORECASE):
        return False
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return False
    docs = body if isinstance(body, list) else [body]
    for doc in docs:
        query = doc.get("query") if isinstance(doc, dict) else None
        if not isinstance(query, str) or _MUTATION_RE.search(query) or re.search(r"\bmutation\s*[({]", query):
            return False
    return True


def _approve(description: str, rule_key: str) -> dict | None:
    """None when approved, else the denial result for the model."""
    from tools.approval import request_tool_approval
    result = request_tool_approval("app_connections", description, rule_key=rule_key)
    if result.get("approved"):
        return None
    return {"error": result.get("message") or "Not approved.", "approved": False,
            "user_consent": result.get("user_consent", False)}


def app_connections(args: dict) -> str:
    resolver = _resolver.get()
    if resolver is None:
        return tool_error(
            "App connections are only available in a signed-in SynthPulse WebUI chat. "
            "There is no personal identity bound to this run, so no connection can be used.")
    action = str(args.get("action") or "list").strip().lower()
    try:
        if action == "list":
            return json.dumps({"connections": resolver.list()}, ensure_ascii=False)
        integration = str(args.get("integration") or "").strip()
        if not integration:
            return tool_error("integration is required (see action='list').")
        connection_id = str(args.get("connection_id") or "").strip() or None
        if action == "request":
            method = str(args.get("method") or "GET").strip().upper()
            if method not in _ALL_METHODS:
                return tool_error(f"Unsupported method {method}.")
            path = str(args.get("path") or "").strip()
            body = args.get("body")
            if method not in _READ_METHODS and not (method == "POST" and _graphql_read(path, body)):
                denied = _approve(f"{method} {integration}{path}", f"{integration}:{method}")
                if denied:
                    return json.dumps(denied, ensure_ascii=False)
            result = resolver.request(integration, connection_id, method, path,
                                      args.get("query") if isinstance(args.get("query"), dict) else None,
                                      body,
                                      args.get("headers") if isinstance(args.get("headers"), dict) else None)
            return json.dumps(result, ensure_ascii=False)
        if action == "mcp":
            method = str(args.get("mcp_method") or "tools/list").strip()
            name = str(args.get("tool") or "").strip() or None
            if method not in {"tools/list", "tools/call"}:
                return tool_error("mcp_method must be tools/list or tools/call.")
            if method == "tools/call":
                if not name:
                    return tool_error("tool is required for tools/call.")
                if not resolver.mcp_tool_is_read_only(integration, connection_id, name):
                    denied = _approve(f"MCP {integration}: {name}", f"{integration}:mcp:{name}")
                    if denied:
                        return json.dumps(denied, ensure_ascii=False)
            arguments = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}
            return json.dumps(resolver.mcp(integration, connection_id, method, name, arguments), ensure_ascii=False)
        return tool_error("action must be list, request or mcp.")
    except PermissionError as exc:
        return tool_error(str(exc) or "Not allowed.")
    except (ValueError, LookupError) as exc:
        return tool_error(str(exc))
    except Exception as exc:  # resolver failures must not leak tracebacks or credentials
        return tool_error(f"The connection request failed: {type(exc).__name__}: {str(exc)[:300]}")


APP_CONNECTIONS_SCHEMA = {
    "name": "app_connections",
    "description": (
        "Use the apps the signed-in person connected under Integrations in the SynthPulse WebUI "
        "(Nango): their own API-key and OAuth connections (for example Lemlist, Fireflies, "
        "Productive, GitHub, Google Calendar) and MCP connectors (Attio, Granola, Krea, Higgsfield). "
        "Credentials are injected server-side and never shown. Start with action='list'. "
        "action='request' calls the provider's REST/GraphQL API: path is relative to the "
        "provider's API base URL (e.g. '/api/team' for Lemlist, '/graphql' for Fireflies, "
        "'/calendar/v3/users/me/calendarList' for Google Calendar). action='mcp' lists or calls "
        "tools of an MCP connector. Calls that change data ask the person for approval first. "
        "If the app is not in the list, ask the person to connect it under Integrations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "request", "mcp"]},
            "integration": {"type": "string", "description": "Integration key from action='list' (provider_config_key)."},
            "connection_id": {"type": "string", "description": "Only needed when the person has several connections for one integration."},
            "method": {"type": "string", "enum": sorted(_ALL_METHODS), "description": "HTTP method for action='request' (default GET)."},
            "path": {"type": "string", "description": "API path starting with '/', relative to the provider base URL."},
            "query": {"type": "object", "description": "Query string parameters."},
            "body": {"description": "JSON body (object/array) or raw string for action='request'."},
            "headers": {"type": "object", "description": "Extra non-auth headers (e.g. API version headers)."},
            "mcp_method": {"type": "string", "enum": ["tools/list", "tools/call"]},
            "tool": {"type": "string", "description": "MCP tool name for tools/call."},
            "arguments": {"type": "object", "description": "MCP tool arguments for tools/call."},
        },
        "required": ["action"],
    },
}


registry.register(
    name="app_connections",
    toolset="app_connections",
    schema=APP_CONNECTIONS_SCHEMA,
    handler=lambda args, **kw: app_connections(args),
    emoji="🔌",
)
