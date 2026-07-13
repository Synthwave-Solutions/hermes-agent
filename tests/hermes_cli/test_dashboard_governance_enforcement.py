from __future__ import annotations

from types import ModuleType, SimpleNamespace
import importlib.util
import sys


def _load_enforcement(monkeypatch):
    fake_fastapi = ModuleType("fastapi")
    fake_fastapi.Request = object
    fake_responses = ModuleType("fastapi.responses")

    class JSONResponse:
        def __init__(self, content, status_code=200):
            self.content = content
            self.status_code = status_code

    fake_responses.JSONResponse = JSONResponse
    monkeypatch.setitem(sys.modules, "fastapi", fake_fastapi)
    monkeypatch.setitem(sys.modules, "fastapi.responses", fake_responses)
    # Load a private copy without touching sys.modules for the enforcement
    # module itself: other tests must keep importing the real-fastapi version.
    spec = importlib.util.find_spec("hermes_cli.dashboard_governance.enforcement")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Session:
    email = "stub@example.test"
    display_name = "Stub User"
    provider = "stub"
    user_id = "stub-user-1"
    org_id = "stub-org-1"


class _URL:
    def __init__(self, path):
        self.path = path


class _Query(dict):
    def get(self, key, default=""):
        return super().get(key, default)


class _Request:
    def __init__(self, policy, *, path="/api/config", method="GET", query=None):
        self.url = _URL(path)
        self.method = method
        self.query_params = _Query(query or {})
        self.state = SimpleNamespace(session=_Session())
        self.app = SimpleNamespace(state=SimpleNamespace(governance_policy_loader=lambda: policy))


def _policy(data):
    from hermes_cli.dashboard_governance.loader import parse_governance_policy

    return parse_governance_policy(data)


def test_governance_decision_allows_route_permission_profile(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy(
        {
            "version": 1,
            "mode": "enforce",
            "users": {
                "stub@example.test": {
                    "grants": {
                        "permissions": ["config:read"],
                        "profiles": ["worker_alpha"],
                        "routes": ["/api/config"],
                    }
                }
            },
        }
    )

    allowed, reason, access = enforcement.governance_decision(
        _Request(policy, query={"profile": "worker_alpha"})
    )

    assert allowed is True
    assert reason == "allowed"
    assert access.has_permission("config:read")


def test_governance_decision_allows_public_api_paths_without_principal(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy({"version": 1, "mode": "enforce"})
    request = _Request(policy, path="/api/status")
    request.state.session = None

    allowed, reason, _ = enforcement.governance_decision(request)

    assert allowed is True
    assert reason == "public"


def test_governance_decision_denies_missing_route(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy(
        {
            "version": 1,
            "mode": "enforce",
            "users": {
                "stub@example.test": {
                    "grants": {
                        "permissions": ["config:read"],
                        "profiles": ["default"],
                        "routes": ["/api/auth/me"],
                    }
                }
            },
        }
    )

    allowed, reason, _ = enforcement.governance_decision(_Request(policy))

    assert allowed is False
    assert reason == "route_not_allowed"


def test_governance_decision_denies_missing_permission(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy(
        {
            "version": 1,
            "mode": "enforce",
            "users": {
                "stub@example.test": {
                    "grants": {
                        "permissions": [],
                        "profiles": ["default"],
                        "routes": ["/api/config"],
                    }
                }
            },
        }
    )

    allowed, reason, _ = enforcement.governance_decision(_Request(policy))

    assert allowed is False
    assert reason == "permission_not_allowed"


def test_governance_decision_denies_forbidden_profile_without_leaking_name(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy(
        {
            "version": 1,
            "mode": "enforce",
            "users": {
                "stub@example.test": {
                    "grants": {
                        "permissions": ["config:read"],
                        "profiles": ["default"],
                        "routes": ["/api/config"],
                    }
                }
            },
        }
    )

    allowed, reason, _ = enforcement.governance_decision(
        _Request(policy, query={"profile": "hidden_beta"})
    )

    assert allowed is False
    assert reason == "profile_not_allowed"


def test_governance_decision_denies_forbidden_profile_path(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy(
        {
            "version": 1,
            "mode": "enforce",
            "users": {
                "stub@example.test": {
                    "grants": {
                        "permissions": ["profiles:read"],
                        "profiles": ["default"],
                        "routes": ["/api/profiles/hidden_beta/soul"],
                    }
                }
            },
        }
    )

    allowed, reason, _ = enforcement.governance_decision(
        _Request(policy, path="/api/profiles/hidden_beta/soul")
    )

    assert allowed is False
    assert reason == "profile_not_allowed"


def test_governance_decision_denies_unknown_catalog_route(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy(
        {
            "version": 1,
            "mode": "enforce",
            "users": {
                "stub@example.test": {
                    "grants": {
                        "permissions": ["*"],
                        "profiles": ["default"],
                        "routes": ["/api/future/new-route"],
                    }
                }
            },
        }
    )

    allowed, reason, _ = enforcement.governance_decision(
        _Request(policy, path="/api/future/new-route")
    )

    assert allowed is False
    assert reason == "unknown_route"


def test_filter_profiles_for_access(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy(
        {
            "version": 1,
            "mode": "enforce",
            "users": {
                "stub@example.test": {
                    "grants": {
                        "permissions": ["profiles:read"],
                        "profiles": ["default", "worker_alpha"],
                        "routes": ["/api/profiles"],
                    }
                }
            },
        }
    )
    _, _, access = enforcement.governance_decision(_Request(policy, path="/api/profiles"))

    profiles = [
        {"name": "default"},
        {"name": "worker_alpha"},
        {"name": "hidden_beta"},
    ]

    assert enforcement.filter_profiles_for_access(profiles, access) == profiles[:2]


def test_governance_effective_access_alias_is_self_route(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy(
        {
            "version": 1,
            "mode": "enforce",
            "users": {
                "stub@example.test": {
                    "grants": {
                        "permissions": [],
                        "profiles": ["default"],
                        "routes": ["/api/governance/effective-access"],
                    }
                }
            },
        }
    )

    allowed, reason, _ = enforcement.governance_decision(
        _Request(policy, path="/api/governance/effective-access")
    )

    assert allowed is True
    assert reason == "allowed"


def test_serialize_effective_access_excludes_tokens(monkeypatch):
    enforcement = _load_enforcement(monkeypatch)
    policy = _policy(
        {
            "version": 1,
            "mode": "enforce",
            "users": {
                "stub@example.test": {
                    "grants": {
                        "permissions": ["governance:read"],
                        "profiles": ["default"],
                        "routes": ["/api/governance/policy"],
                    }
                }
            },
        }
    )
    _, _, access = enforcement.governance_decision(
        _Request(policy, path="/api/governance/policy")
    )

    payload = enforcement.serialize_effective_access(access)

    assert payload["is_admin"] is True
    assert payload["permissions"] == ["governance:read"]
    assert "token" not in str(payload).lower()
