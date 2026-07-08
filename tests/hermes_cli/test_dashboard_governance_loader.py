from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def test_missing_policy_defaults_to_off(tmp_path):
    from hermes_cli.dashboard_governance.loader import load_governance_policy

    policy = load_governance_policy(path=tmp_path / "missing.yaml")

    assert policy.mode == "off"
    assert policy.default_effect == "deny"
    assert policy.roles == {}


def test_policy_file_parses_whitelist_defaults(tmp_path):
    from hermes_cli.dashboard_governance.loader import load_governance_policy

    path = tmp_path / "dashboard-governance.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "enforce",
                "default_effect": "deny",
                "bootstrap_admins": ["owner@example.com"],
                "roles": {
                    "operator": {
                        "grants": {
                            "permissions": ["sessions:read"],
                            "profiles": ["default", "eng-ops"],
                            "routes": ["/api/sessions"],
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    policy = load_governance_policy(path=path)

    assert policy.mode == "enforce"
    assert policy.default_effect == "deny"
    assert policy.bootstrap_admins == ("owner@example.com",)
    assert policy.roles["operator"].grants.permissions == frozenset({"sessions:read"})
    assert policy.roles["operator"].grants.profiles == frozenset({"default", "eng-ops"})


def test_invalid_mode_is_rejected(tmp_path):
    from hermes_cli.dashboard_governance.loader import GovernancePolicyError, load_governance_policy

    path = tmp_path / "dashboard-governance.yaml"
    path.write_text("version: 1\nmode: maybe\n", encoding="utf-8")

    with pytest.raises(GovernancePolicyError):
        load_governance_policy(path=path)


def test_policy_path_can_come_from_dashboard_config(tmp_path):
    from hermes_cli.dashboard_governance.loader import resolve_policy_path

    configured = tmp_path / "custom.yaml"
    cfg = {"dashboard": {"governance": {"policy_file": str(configured)}}}

    assert resolve_policy_path(config=cfg, hermes_home=tmp_path) == configured
    assert resolve_policy_path(config={}, hermes_home=tmp_path) == tmp_path / "dashboard-governance.yaml"


def test_save_policy_validates_and_writes_atomically(tmp_path):
    from hermes_cli.dashboard_governance.loader import load_governance_policy, save_governance_policy

    path = tmp_path / "nested" / "dashboard-governance.yaml"
    payload = {
        "version": 1,
        "mode": "enforce",
        "default_effect": "deny",
        "users": {
            "Owner@Example.com": {
                "grants": {
                    "permissions": ["governance:read"],
                    "profiles": ["default"],
                    "routes": ["/api/governance/policy"],
                }
            }
        },
    }

    saved = save_governance_policy(payload, path=path)

    assert saved == path
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    policy = load_governance_policy(path=path)
    assert policy.mode == "enforce"
    assert "owner@example.com" in policy.users


def test_save_policy_rejects_invalid_policy_without_overwriting(tmp_path):
    from hermes_cli.dashboard_governance.loader import GovernancePolicyError, save_governance_policy

    path = tmp_path / "dashboard-governance.yaml"
    path.write_text("version: 1\nmode: enforce\ndefault_effect: deny\n", encoding="utf-8")

    with pytest.raises(GovernancePolicyError):
        save_governance_policy({"version": 1, "mode": "maybe"}, path=path)

    assert "mode: enforce" in path.read_text(encoding="utf-8")
