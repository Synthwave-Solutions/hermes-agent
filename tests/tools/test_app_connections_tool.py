"""app_connections tool: fail closed when unbound, approval for writes, GraphQL read detection."""
import json

from tests.tools._child_env_fixtures import child_env  # noqa: F401
from tools import app_connections_tool as m


class Fake:
    def __init__(self):
        self.calls = []

    def list(self):
        return [{"integration": "x"}]

    def request(self, *a):
        self.calls.append(("request",) + a)
        return {"status": 200}

    def mcp(self, *a):
        self.calls.append(("mcp",) + a)
        return {"ok": True}

    def mcp_tool_is_read_only(self, integration, connection_id, name):
        return name == "search"


def test_unbound_refuses():
    assert "error" in json.loads(m.app_connections({"action": "list"}))


def test_reads_run_and_writes_need_approval(monkeypatch):
    fake = Fake()
    asked = []
    monkeypatch.setattr(m, "_approve", lambda d, k: asked.append(k) or {"error": "denied", "approved": False})
    token = m.bind_app_connections(fake)
    try:
        assert json.loads(m.app_connections({"action": "request", "integration": "x", "path": "/a"}))["status"] == 200
        assert json.loads(m.app_connections({"action": "request", "integration": "x", "method": "POST", "path": "/graphql",
                                             "body": {"query": "{ me { id } }"}}))["status"] == 200
        assert json.loads(m.app_connections({"action": "request", "integration": "x", "method": "POST", "path": "/graphql",
                                             "body": {"query": "mutation { drop }"}}))["approved"] is False
        assert json.loads(m.app_connections({"action": "request", "integration": "x", "method": "DELETE", "path": "/a"}))["approved"] is False
        assert json.loads(m.app_connections({"action": "mcp", "integration": "x", "mcp_method": "tools/call", "tool": "search"}))["ok"]
        assert json.loads(m.app_connections({"action": "mcp", "integration": "x", "mcp_method": "tools/call", "tool": "create"}))["approved"] is False
    finally:
        m.reset_app_connections(token)
    assert asked == ["x:POST", "x:DELETE", "x:mcp:create"]
    assert len([c for c in fake.calls if c[0] == "request"]) == 2


def test_personal_cli_env_reaches_child_env_only_while_bound(child_env, monkeypatch):
    from tools.environments import local
    from tools.environments.local import _sanitize_subprocess_env
    monkeypatch.setattr(local, "_prepend_hermes_bin_dir", lambda path: path)

    class WithEnv(Fake):
        def cli_env(self):
            return {"PRODUCTIVE_API_KEY": "personal-key"}

    class Broken(Fake):
        def cli_env(self):
            raise RuntimeError("nango down")

    assert "PRODUCTIVE_API_KEY" not in _sanitize_subprocess_env({"PATH": "/usr/bin"})
    token = m.bind_app_connections(WithEnv())
    try:
        env = _sanitize_subprocess_env({"PATH": "/usr/bin", "PRODUCTIVE_API_KEY": "host-key"})
        assert env["PRODUCTIVE_API_KEY"] == "personal-key"
    finally:
        m.reset_app_connections(token)
    token = m.bind_app_connections(Broken())
    try:
        assert "PATH" in _sanitize_subprocess_env({"PATH": "/usr/bin"})
    finally:
        m.reset_app_connections(token)
