"""Web server session updates: behavior contracts."""
from __future__ import annotations

import tests.hermes_cli.test_web_server as _support

pytest_plugins = ["tests.hermes_cli.test_web_server"]


class TestSessionPatchUnread:
    """PATCH /api/sessions/{id} with {"unread": bool} marks the session
    read/unread, and GET /api/sessions surfaces the derived flag."""

    @_support.pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home, mutation_owner):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            _support.pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(
            hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db"
        )

        self.owner = mutation_owner
        self.client = TestClient(app)
        self.auth_client = TestClient(app)
        self.auth_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="s1", source="cli")
            db.append_message(session_id="s1", role="user", content="hi")
            db.set_session_read("s1")  # start read, like a conversation you opened
        finally:
            db.close()

    def _patch(self, payload):
        from uuid import uuid4
        from gateway.session_authority import LiveSession
        self.owner.sessions.setdefault('s1', LiveSession(None, 'route'))
        row = self.owner.db.get_session('s1')
        return self.auth_client.patch('/api/sessions/s1', json={
            **payload, 'request_id': uuid4().hex, 'expected_revision': row['runtime_revision'],
            'expected_generation': row['runtime_generation']})

    def test_patch_unread_true_marks_row_unread(self):
        resp = self._patch({"unread": True})
        assert resp.status_code == 200
        assert resp.json()["unread"] is True

        rows = self.auth_client.get("/api/sessions?limit=100").json()["sessions"]
        assert next(s for s in rows if s["id"] == "s1")["unread"] is True

    def test_patch_unread_false_marks_row_read(self):
        self._patch({"unread": True})
        resp = self._patch({"unread": False})
        assert resp.status_code == 200
        assert resp.json()["unread"] is False

        rows = self.auth_client.get("/api/sessions?limit=100").json()["sessions"]
        assert next(s for s in rows if s["id"] == "s1")["unread"] is False


    def test_patch_unread_rejects_non_bool(self):
        # NB: pydantic v2 coerces "yes"/"no"/"1"/"0"/"on"/"off" to bool, so use
        # a string outside the accepted set to prove validation rejects it.
        resp = self._patch({"unread": "maybe"})
        assert resp.status_code == 422  # pydantic validation

    def test_patch_hidden_updates_persisted_session_without_live_runtime(self):
        resp = self._patch({"hidden": True})
        assert resp.status_code == 200
        assert resp.json()["hidden"] is True

        rows = self.auth_client.get("/api/sessions?limit=100").json()["sessions"]
        assert all(s["id"] != "s1" for s in rows)

        restored = self._patch(
            {"hidden": False}
        )
        assert restored.status_code == 200
        rows = self.auth_client.get("/api/sessions?limit=100").json()["sessions"]
        assert bool(next(s for s in rows if s["id"] == "s1")["hidden"]) is False


class TestSubmittedCustomEndpointSurvivesAssignment:
    """#115661 follow-up: a bare-``custom`` main-slot pick carries the submitted endpoint as the
    current one (see ``_validated_main_model_selection``). Once the switch's credential step
    re-resolves that target, an env endpoint (``CUSTOM_BASE_URL`` / ``OPENROUTER_BASE_URL``) could
    replace what the user typed and had persisted."""

    def test_submitted_custom_endpoint_wins_over_an_env_endpoint(self, monkeypatch):
        from hermes_cli.web_server_config import _apply_main_model_assignment, _validated_main_model_selection

        monkeypatch.setenv("CUSTOM_BASE_URL", "http://127.0.0.1:9999/v1")
        monkeypatch.setattr(
            "hermes_cli.models_validate.validate_requested_model",
            lambda *a, **k: {"accepted": True, "persist": True, "recognized": True, "message": None})
        monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
        monkeypatch.setattr("hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None)

        cfg = {"model": {"provider": "openrouter", "default": "m"}}
        result = _validated_main_model_selection(
            cfg, "custom", "qwen3:8b", "https://api.anthropic.com", "submitted-key")

        assert result.base_url == "https://api.anthropic.com"
        # The wire protocol follows the endpoint that gets persisted, not the displaced env host.
        assert result.api_mode == "anthropic_messages"
        applied = _apply_main_model_assignment(cfg.get("model", {}), result, "submitted-key")
        assert applied["base_url"] == "https://api.anthropic.com"
        assert applied["api_mode"] == "anthropic_messages"
        assert applied["api_key"] == "submitted-key"
