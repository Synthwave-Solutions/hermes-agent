"""Managed-service viewer policy must fail closed without touching screen state."""
import asyncio
import pytest
import yaml
from tools.bot_desktop import dashboard_viewer, lease, runtime


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "launch"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    lease._reset_for_tests()
    yield h
    lease._reset_for_tests()


def config(home, value):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"bot_desktop": {"dashboard_viewer": value}}))


def rpc(name, params):
    import tui_gateway.server as server
    return server.handle_request({"jsonrpc": "2.0", "id": 1, "method": name, "params": params})


@pytest.mark.parametrize("value", [False, "true", 1, "yes", None, {}])
def test_only_exact_true_opens_viewer(home, value):
    config(home, value)
    assert not dashboard_viewer.enabled()
    config(home, True)
    assert dashboard_viewer.enabled()


def test_unreadable_config_fails_closed_and_restores_request_scope(home, tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override, get_hermes_home
    from hermes_cli import config as cfg
    other = tmp_path / "other"
    config(other, True)
    token = set_hermes_home_override(other)
    try:
        assert not dashboard_viewer.enabled(), "request profile opened launch-profile viewer"
        monkeypatch.setattr(cfg, "load_config_readonly", lambda: (_ for _ in ()).throw(OSError("unreadable")))
        assert not dashboard_viewer.enabled()
        assert get_hermes_home() == other
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("name,params", [
    ("display.observe", {}), ("display.thumbnail", {}),
    ("display.lease.acquire", {"viewer_id": "forged"}),
    ("display.lease.release", {"viewer_id": "held"}),
    ("display.lease.release", {"force": True}),
    ("display.stop", {"force": True}),
    ("display.install", {}), ("display.switchSandboxImage", {"approve": True}),
])
def test_denied_rpc_preserves_human_lease_and_never_touches_runtime(home, monkeypatch, name, params):
    from tools.bot_desktop import install, thumbnail
    def forbidden(*a, **kw):
        pytest.fail("denied RPC reached runtime")
    for obj, attr in [(runtime, "stop"), (runtime, "rfb_socket_path"),
                      (install, "claim"), (thumbnail, "thumbnail_data_url")]:
        monkeypatch.setattr(obj, attr, forbidden)
    lease.acquire("held")
    before = lease.get()
    result = rpc(name, params)
    assert result["error"]["data"]["code"] == "dashboard_viewer_off", result
    assert lease.get() == before


def test_request_profile_cannot_opt_in_rpc(home, tmp_path, monkeypatch):
    import tui_gateway.server as server
    other = tmp_path / "named"
    config(other, True)
    monkeypatch.setattr(server, "_profile_home", lambda name: str(other))
    result = rpc("display.observe", {"profile": "named"})
    assert result["error"]["data"]["code"] == "dashboard_viewer_off"


def test_operator_status_start_and_plain_stop_still_work(home, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "start", lambda: calls.append("start"))
    monkeypatch.setattr(runtime, "stop", lambda: calls.append("stop") or False)
    for name in ["display.status", "display.start", "display.stop"]:
        assert "result" in rpc(name, {}), name
    assert calls == ["start", "stop"]


def test_default_route_not_mounted_and_config_disable_blocks_existing_route(home, monkeypatch):
    from fastapi import FastAPI
    from hermes_cli.web_routers import display
    app = FastAPI()
    assert not display.register(app)
    assert not any(getattr(r, "path", "") == "/api/display/ws" for r in app.routes)
    config(home, True)
    assert display.register(app)
    assert any(getattr(r, "path", "") == "/api/display/ws" for r in app.routes)
    config(home, False)
    class WS:
        closed = None
        async def close(self, code): self.closed = code
        async def accept(self): pytest.fail("disabled route accepted")
    ws = WS()
    asyncio.run(display.display_ws(ws))
    assert ws.closed == display._CLOSE_NOT_ALLOWED
