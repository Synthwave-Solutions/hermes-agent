from __future__ import annotations

from pathlib import Path

import pytest
import yaml

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


@pytest.fixture
def isolated_profiles(tmp_path, monkeypatch, _isolate_hermes_home):
    from hermes_constants import get_hermes_home
    from hermes_cli import profiles

    default_home = get_hermes_home()
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker_alpha"
    hidden_home = profiles_root / "hidden_beta"
    for home in (default_home, worker_home, hidden_home):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "worker_alpha": worker_home, "hidden_beta": hidden_home}


@pytest.fixture
def governed_client(isolated_profiles):
    clear_providers()
    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    try:
        yield client
    finally:
        clear_providers()
        web_server.app.state.bound_host = prev_host
        web_server.app.state.bound_port = prev_port
        web_server.app.state.auth_required = prev_required


def _write_policy(home: Path, user_grants: dict, *, roles: dict | None = None) -> None:
    (home / "dashboard-governance.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "enforce",
                "default_effect": "deny",
                "roles": roles or {},
                "users": {
                    "stub@example.test": {
                        "grants": user_grants,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _login(client: TestClient) -> None:
    first = client.get("/auth/login?provider=stub", follow_redirects=False)
    assert first.status_code == 302
    state = first.headers["location"].split("state=")[1]
    callback = client.get(
        f"/auth/callback?code=stub_code&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code == 302


def test_auth_me_includes_governance_summary(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["profiles:read"],
            "profiles": ["default"],
            "routes": ["/api/auth/me", "/api/governance/me"],
        },
    )
    _login(governed_client)

    resp = governed_client.get("/api/auth/me")

    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "stub@example.test"
    assert body["governance"]["mode"] == "enforce"
    assert body["governance"]["profiles"] == ["default"]
    assert body["governance"]["permissions"] == ["profiles:read"]
    assert "access_token" not in str(body).lower()
    assert "refresh_token" not in str(body).lower()


def test_forbidden_governed_route_returns_403(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": [],
            "profiles": ["default"],
            "routes": ["/api/auth/me"],
        },
    )
    _login(governed_client)

    resp = governed_client.get("/api/config")

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Forbidden"


def test_forbidden_profile_query_returns_403(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["config:read"],
            "profiles": ["default"],
            "routes": ["/api/config"],
        },
    )
    _login(governed_client)

    resp = governed_client.get("/api/config", params={"profile": "worker_alpha"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Forbidden"
    assert "worker_alpha" not in resp.text


def test_profiles_listing_filters_to_allowed_profiles(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["profiles:read"],
            "profiles": ["default", "worker_alpha"],
            "routes": ["/api/profiles"],
        },
    )
    _login(governed_client)

    resp = governed_client.get("/api/profiles")

    assert resp.status_code == 200
    names = {profile["name"] for profile in resp.json()["profiles"]}
    assert names == {"default", "worker_alpha"}


def test_policy_route_requires_governance_read(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["profiles:read"],
            "profiles": ["default"],
            "routes": ["/api/governance/policy"],
        },
    )
    _login(governed_client)

    resp = governed_client.get("/api/governance/policy")

    assert resp.status_code == 403


def test_policy_route_allows_governance_reader(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["governance:read"],
            "profiles": ["default"],
            "routes": ["/api/governance/policy"],
        },
    )
    _login(governed_client)

    resp = governed_client.get("/api/governance/policy")

    assert resp.status_code == 200
    body = resp.json()
    assert body["policy"]["mode"] == "enforce"
    assert body["effective_access"]["permissions"] == ["governance:read"]


def test_model_options_are_filtered_by_provider_and_model_grants(
    governed_client, isolated_profiles, monkeypatch
):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["model:read"],
            "profiles": ["default"],
            "routes": ["/api/model/options"],
            "models": {
                "providers": ["openrouter"],
                "models": ["anthropic/claude-sonnet-4.6"],
            },
        },
    )
    _login(governed_client)

    def fake_payload(*_args, **_kwargs):
        return {
            "providers": [
                {
                    "slug": "openrouter",
                    "name": "OpenRouter",
                    "models": ["anthropic/claude-sonnet-4.6", "openai/gpt-5.5"],
                    "authenticated": True,
                },
                {
                    "slug": "anthropic",
                    "name": "Anthropic",
                    "models": ["claude-opus-4-6"],
                    "authenticated": True,
                },
            ]
        }

    monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: {})
    monkeypatch.setattr("hermes_cli.inventory.build_models_payload", fake_payload)

    resp = governed_client.get("/api/model/options")

    assert resp.status_code == 200
    body = resp.json()
    assert [provider["slug"] for provider in body["providers"]] == ["openrouter"]
    assert body["providers"][0]["models"] == ["anthropic/claude-sonnet-4.6"]


def test_model_set_blocks_ungranted_provider_model(
    governed_client, isolated_profiles, monkeypatch
):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["model:write"],
            "profiles": ["default"],
            "routes": ["/api/model/set"],
            "models": {
                "providers": ["openrouter"],
                "models": ["anthropic/claude-sonnet-4.6"],
            },
        },
    )
    _login(governed_client)
    monkeypatch.setattr("hermes_cli.model_cost_guard.expensive_model_warning", lambda *_a, **_k: None)

    resp = governed_client.post(
        "/api/model/set",
        json={"scope": "main", "provider": "anthropic", "model": "claude-opus-4-6"},
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "model_provider_not_allowed"


def test_model_set_allows_granted_provider_model(
    governed_client, isolated_profiles, monkeypatch
):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["model:write"],
            "profiles": ["default"],
            "routes": ["/api/model/set"],
            "models": {
                "providers": ["openrouter"],
                "models": ["anthropic/claude-sonnet-4.6"],
            },
        },
    )
    _login(governed_client)
    monkeypatch.setattr("hermes_cli.model_cost_guard.expensive_model_warning", lambda *_a, **_k: None)

    resp = governed_client.post(
        "/api/model/set",
        json={"scope": "main", "provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["provider"] == "openrouter"
    assert body["model"] == "anthropic/claude-sonnet-4.6"
