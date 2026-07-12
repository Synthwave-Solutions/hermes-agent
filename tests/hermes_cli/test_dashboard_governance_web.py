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


def _write_policy(home: Path, user_grants: dict, *, roles: dict | None = None, mode: str = "enforce") -> None:
    (home / "dashboard-governance.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": mode,
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


def test_governance_users_and_groups_endpoints_return_structured_policy(governed_client, isolated_profiles):
    policy_path = isolated_profiles["default"] / "dashboard-governance.yaml"
    policy_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "enforce",
                "default_effect": "deny",
                "groups": {
                    "operators": {
                        "roles": ["reader"],
                        "sso_groups": ["ops@example.test"],
                        "grants": {"profiles": ["default"]},
                    }
                },
                "users": {
                    "stub@example.test": {
                        "grants": {
                            "permissions": ["governance:read"],
                            "profiles": ["default"],
                            "routes": ["/api/governance/users", "/api/governance/groups"],
                        }
                    },
                    "operator@example.test": {"groups": ["operators"]},
                },
            }
        ),
        encoding="utf-8",
    )
    _login(governed_client)

    users = governed_client.get("/api/governance/users")
    groups = governed_client.get("/api/governance/groups")

    assert users.status_code == 200
    assert groups.status_code == 200
    assert users.json()["users"]["operator@example.test"]["groups"] == ["operators"]
    assert groups.json()["groups"]["operators"]["roles"] == ["reader"]


def test_governance_simulate_returns_route_decision(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["governance:preview"],
            "profiles": ["default"],
            "routes": ["/api/governance/simulate"],
        },
    )
    _login(governed_client)

    resp = governed_client.post(
        "/api/governance/simulate",
        json={
            "policy": {
                "version": 1,
                "mode": "enforce",
                "default_effect": "deny",
                "users": {
                    "operator@example.test": {
                        "grants": {
                            "permissions": ["config:read"],
                            "profiles": ["default"],
                            "routes": ["/api/config"],
                        }
                    }
                },
            },
            "subject": {"email": "operator@example.test", "user_id": "operator-1"},
            "request": {"path": "/api/config", "method": "GET", "profile": "default"},
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is True
    assert body["reason"] == "allowed"
    assert body["required_permission"] == "config:read"


def test_governance_preview_returns_effective_access_without_saving(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["governance:preview"],
            "profiles": ["default"],
            "routes": ["/api/governance/preview"],
        },
    )
    _login(governed_client)
    policy_path = isolated_profiles["default"] / "dashboard-governance.yaml"
    before = policy_path.read_text(encoding="utf-8")

    resp = governed_client.post(
        "/api/governance/preview",
        json={
            "policy": {
                "version": 1,
                "mode": "enforce",
                "default_effect": "deny",
                "users": {
                    "operator@example.test": {
                        "grants": {
                            "permissions": ["files:read"],
                            "profiles": ["worker_alpha"],
                            "routes": ["/api/files"],
                        }
                    }
                },
            },
            "subject": {
                "email": "operator@example.test",
                "provider": "stub",
                "user_id": "operator-1",
            },
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["effective_access"]["mode"] == "enforce"
    assert body["effective_access"]["permissions"] == ["files:read"]
    assert body["effective_access"]["profiles"] == ["worker_alpha"]
    assert policy_path.read_text(encoding="utf-8") == before


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


def _write_admin_policy(home: Path) -> None:
    _write_policy(
        home,
        {
            "permissions": ["governance:write"],
            "profiles": ["default"],
            "routes": [
                "/api/governance/policy",
                "/api/governance/groups",
                "/api/governance/groups/*",
                "/api/governance/users/*",
            ],
        },
    )


def _read_policy(home: Path) -> dict:
    return yaml.safe_load((home / "dashboard-governance.yaml").read_text(encoding="utf-8"))


def _policy_change_events(*, action: str | None = None) -> list[dict]:
    from hermes_cli.dashboard_governance.audit import read_audit_events

    events = [event for event in read_audit_events(limit=100) if event["event"] == "policy_change"]
    if action is not None:
        events = [event for event in events if event["extra"]["action"] == action]
    return events


def test_governance_group_create_update_delete_happy_path(governed_client, isolated_profiles):
    _write_admin_policy(isolated_profiles["default"])
    _login(governed_client)

    created = governed_client.post(
        "/api/governance/groups",
        json={"name": "operators", "group": {"roles": ["reader"], "grants": {"profiles": ["default"]}}},
    )
    assert created.status_code == 200
    assert created.json()["ok"] is True
    assert _read_policy(isolated_profiles["default"])["groups"]["operators"]["roles"] == ["reader"]

    updated = governed_client.put(
        "/api/governance/groups/operators",
        json={"roles": ["reader", "writer"], "grants": {"profiles": ["default", "worker_alpha"]}},
    )
    assert updated.status_code == 200
    assert updated.json()["groups"]["operators"]["roles"] == ["reader", "writer"]
    assert _read_policy(isolated_profiles["default"])["groups"]["operators"]["roles"] == ["reader", "writer"]

    deleted = governed_client.delete("/api/governance/groups/operators")
    assert deleted.status_code == 200
    assert deleted.json()["groups"] == {}
    assert _read_policy(isolated_profiles["default"])["groups"] == {}

    assert len(_policy_change_events(action="group_create")) == 1
    assert len(_policy_change_events(action="group_update")) == 1
    assert len(_policy_change_events(action="group_delete")) == 1


def test_governance_group_create_writes_policy_change_audit_event(governed_client, isolated_profiles):
    _write_admin_policy(isolated_profiles["default"])
    _login(governed_client)

    resp = governed_client.post(
        "/api/governance/groups",
        json={"name": "operators", "group": {"roles": ["reader"]}},
    )

    assert resp.status_code == 200
    events = _policy_change_events(action="group_create")
    assert len(events) == 1
    event = events[0]
    assert event["method"] == "POST"
    assert event["path"] == "/api/governance/groups"
    assert event["reason"] == "group_create"
    assert event["extra"]["target"] == "operators"
    assert event["extra"]["before"] is None
    assert event["extra"]["after"] == {"roles": ["reader"]}
    assert event["subject_email_hash"]
    assert "stub@example.test" not in str(event)


def test_governance_group_mutations_forbidden_for_non_admin(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["governance:read"],
            "profiles": ["default"],
            "routes": [
                "/api/governance/groups",
                "/api/governance/groups/*",
                "/api/governance/users/*",
            ],
        },
    )
    _login(governed_client)

    created = governed_client.post("/api/governance/groups", json={"name": "operators"})
    upserted = governed_client.put("/api/governance/users/other@example.test", json={"roles": []})

    assert created.status_code == 403
    assert created.json()["detail"] == "Forbidden"
    assert upserted.status_code == 403
    assert upserted.json()["detail"] == "Forbidden"
    assert _policy_change_events() == []


def test_governance_mutations_forbidden_for_non_admin_in_report_only_mode(governed_client, isolated_profiles):
    """report_only must stay a dry-run: denied requests pass the middleware,
    but policy mutations must never execute without governance:write."""
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["governance:read"],
            "profiles": ["default"],
            "routes": ["*"],
        },
        mode="report_only",
    )
    _login(governed_client)
    before = (isolated_profiles["default"] / "dashboard-governance.yaml").read_text(encoding="utf-8")

    created = governed_client.post("/api/governance/groups", json={"name": "operators"})
    upserted = governed_client.put(
        "/api/governance/users/stub@example.test",
        json={"grants": {"permissions": ["governance:write"]}},
    )
    replaced = governed_client.put(
        "/api/governance/policy",
        json={"version": 1, "mode": "enforce", "default_effect": "deny"},
    )

    assert created.status_code == 403
    assert upserted.status_code == 403
    assert replaced.status_code == 403
    assert (isolated_profiles["default"] / "dashboard-governance.yaml").read_text(encoding="utf-8") == before
    assert _policy_change_events() == []


def test_governance_mutations_allowed_for_admin_in_report_only_mode(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["governance:write"],
            "profiles": ["default"],
            "routes": ["*"],
        },
        mode="report_only",
    )
    _login(governed_client)

    created = governed_client.post("/api/governance/groups", json={"name": "operators"})

    assert created.status_code == 200
    assert _read_policy(isolated_profiles["default"])["groups"]["operators"] == {}


def test_governance_mutations_forbidden_without_policy_file(governed_client, isolated_profiles):
    """With governance off (no policy file), a session-holder must not be able
    to bootstrap a policy that grants themselves governance:write."""
    _login(governed_client)

    replaced = governed_client.put(
        "/api/governance/policy",
        json={"version": 1, "mode": "enforce", "default_effect": "deny"},
    )
    created = governed_client.post("/api/governance/groups", json={"name": "operators"})

    assert replaced.status_code == 403
    assert created.status_code == 403
    assert not (isolated_profiles["default"] / "dashboard-governance.yaml").exists()


def test_put_governance_policy_if_match_precondition(governed_client, isolated_profiles):
    _write_policy(
        isolated_profiles["default"],
        {
            "permissions": ["governance:read", "governance:write"],
            "profiles": ["default"],
            "routes": ["/api/governance/policy"],
        },
    )
    _login(governed_client)

    loaded = governed_client.get("/api/governance/policy")
    assert loaded.status_code == 200
    etag = loaded.json()["etag"]
    assert etag

    body = _read_policy(isolated_profiles["default"])
    body["groups"] = {"operators": {"roles": ["reader"]}}

    stale = governed_client.put(
        "/api/governance/policy",
        json=body,
        headers={"If-Match": "0" * 64},
    )
    assert stale.status_code == 412
    assert stale.json()["detail"]["error"] == "policy_conflict"
    assert "operators" not in (_read_policy(isolated_profiles["default"]).get("groups") or {})

    fresh = governed_client.put("/api/governance/policy", json=body, headers={"If-Match": etag})
    assert fresh.status_code == 200
    assert fresh.json()["etag"] != etag
    assert _read_policy(isolated_profiles["default"])["groups"]["operators"]["roles"] == ["reader"]


def test_governance_group_create_rejects_invalid_payload(governed_client, isolated_profiles):
    _write_admin_policy(isolated_profiles["default"])
    _login(governed_client)
    before = (isolated_profiles["default"] / "dashboard-governance.yaml").read_text(encoding="utf-8")

    missing_name = governed_client.post("/api/governance/groups", json={"group": {"roles": []}})
    bad_roles = governed_client.post(
        "/api/governance/groups",
        json={"name": "operators", "group": {"roles": "reader"}},
    )
    bad_grants = governed_client.post(
        "/api/governance/groups",
        json={"name": "operators", "group": {"grants": ["profiles"]}},
    )

    assert missing_name.status_code == 400
    assert missing_name.json()["detail"]["error"] == "invalid_payload"
    assert bad_roles.status_code == 400
    assert bad_roles.json()["detail"]["error"] == "invalid_payload"
    assert bad_grants.status_code == 400
    assert bad_grants.json()["detail"]["error"] == "invalid_payload"
    assert (isolated_profiles["default"] / "dashboard-governance.yaml").read_text(encoding="utf-8") == before
    assert _policy_change_events() == []


def test_governance_group_create_conflicts_on_existing_group(governed_client, isolated_profiles):
    _write_admin_policy(isolated_profiles["default"])
    _login(governed_client)

    first = governed_client.post("/api/governance/groups", json={"name": "operators"})
    second = governed_client.post("/api/governance/groups", json={"name": "operators"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "group_exists"


def test_governance_group_update_and_delete_unknown_group_returns_404(governed_client, isolated_profiles):
    _write_admin_policy(isolated_profiles["default"])
    _login(governed_client)

    updated = governed_client.put("/api/governance/groups/ghost", json={"roles": []})
    deleted = governed_client.delete("/api/governance/groups/ghost")

    assert updated.status_code == 404
    assert updated.json()["detail"]["error"] == "group_not_found"
    assert deleted.status_code == 404
    assert deleted.json()["detail"]["error"] == "group_not_found"
    assert _policy_change_events() == []


def test_governance_user_upsert_and_delete_happy_path(governed_client, isolated_profiles):
    _write_admin_policy(isolated_profiles["default"])
    _login(governed_client)

    created = governed_client.put(
        "/api/governance/users/Operator@Example.Test",
        json={"roles": ["reader"], "groups": ["operators"], "grants": {"profiles": ["default"]}},
    )
    assert created.status_code == 200
    assert created.json()["ok"] is True
    saved = _read_policy(isolated_profiles["default"])["users"]["operator@example.test"]
    assert saved["roles"] == ["reader"]
    assert saved["groups"] == ["operators"]

    updated = governed_client.put(
        "/api/governance/users/operator@example.test",
        json={"roles": ["writer"]},
    )
    assert updated.status_code == 200
    assert _read_policy(isolated_profiles["default"])["users"]["operator@example.test"] == {"roles": ["writer"]}

    deleted = governed_client.delete("/api/governance/users/operator@example.test")
    assert deleted.status_code == 200
    assert "operator@example.test" not in _read_policy(isolated_profiles["default"])["users"]

    assert len(_policy_change_events(action="user_create")) == 1
    assert len(_policy_change_events(action="user_update")) == 1
    events = _policy_change_events(action="user_delete")
    assert len(events) == 1
    assert events[0]["extra"]["target"] == "operator@example.test"
    assert events[0]["extra"]["before"] == {"roles": ["writer"]}
    assert events[0]["extra"]["after"] is None


def test_governance_user_upsert_rejects_invalid_payload(governed_client, isolated_profiles):
    _write_admin_policy(isolated_profiles["default"])
    _login(governed_client)

    bad_email = governed_client.put("/api/governance/users/notanemail", json={"roles": []})
    bad_groups = governed_client.put(
        "/api/governance/users/operator@example.test",
        json={"groups": "operators"},
    )

    assert bad_email.status_code == 400
    assert bad_email.json()["detail"]["error"] == "invalid_payload"
    assert bad_groups.status_code == 400
    assert bad_groups.json()["detail"]["error"] == "invalid_payload"
    assert _policy_change_events() == []


def test_governance_user_delete_unknown_user_returns_404(governed_client, isolated_profiles):
    _write_admin_policy(isolated_profiles["default"])
    _login(governed_client)

    resp = governed_client.delete("/api/governance/users/ghost@example.test")

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "user_not_found"
    assert _policy_change_events() == []


def test_put_governance_policy_writes_policy_change_audit_event(governed_client, isolated_profiles):
    _write_admin_policy(isolated_profiles["default"])
    _login(governed_client)
    body = _read_policy(isolated_profiles["default"])
    body["groups"] = {"operators": {"roles": ["reader"]}}

    resp = governed_client.put("/api/governance/policy", json=body)

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    events = _policy_change_events(action="policy_replace")
    assert len(events) == 1
    event = events[0]
    assert event["extra"]["target"] == "policy"
    assert event["extra"]["before"]["groups"] == []
    assert event["extra"]["after"]["groups"] == ["operators"]
    assert event["extra"]["before"]["mode"] == "enforce"
    assert event["extra"]["after"]["mode"] == "enforce"
