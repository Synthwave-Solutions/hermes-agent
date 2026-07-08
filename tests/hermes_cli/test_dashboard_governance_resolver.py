from __future__ import annotations

import yaml


def _policy_from_dict(data):
    from hermes_cli.dashboard_governance.loader import parse_governance_policy

    return parse_governance_policy(data)


def test_unknown_user_denied_by_default():
    from hermes_cli.dashboard_governance.resolver import GovernanceSubject, resolve_effective_access

    policy = _policy_from_dict({"version": 1, "mode": "enforce", "default_effect": "deny"})
    access = resolve_effective_access(
        policy,
        GovernanceSubject(email="unknown@example.com", display_name="Unknown", provider="google"),
    )

    assert not access.has_permission("sessions:read")
    assert not access.is_profile_allowed("default")
    assert access.permissions == frozenset()


def test_bootstrap_admin_gets_wildcard_access():
    from hermes_cli.dashboard_governance.resolver import GovernanceSubject, resolve_effective_access

    policy = _policy_from_dict(
        {
            "version": 1,
            "mode": "enforce",
            "default_effect": "deny",
            "bootstrap_admins": ["owner@example.com"],
        }
    )
    access = resolve_effective_access(
        policy,
        GovernanceSubject(email="OWNER@example.com", display_name="Owner", provider="google"),
    )

    assert access.has_permission("anything:anywhere")
    assert access.is_profile_allowed("finance")
    assert "bootstrap_admin" in access.grant_sources


def test_user_group_role_grants_union():
    from hermes_cli.dashboard_governance.resolver import GovernanceSubject, resolve_effective_access

    policy = _policy_from_dict(
        {
            "version": 1,
            "mode": "enforce",
            "roles": {
                "viewer": {"grants": {"permissions": ["sessions:read"], "profiles": ["default"]}},
                "operator": {"grants": {"permissions": ["chat:use"], "profiles": ["ops"]}},
            },
            "groups": {
                "sw-ops": {"roles": ["operator"], "sso_groups": ["google-ops"], "grants": {"permissions": ["logs:read"]}}
            },
            "users": {
                "user@example.com": {"roles": ["viewer"], "groups": ["sw-ops"], "grants": {"permissions": ["mcp:read"], "profiles": ["custom"]}}
            },
        }
    )
    access = resolve_effective_access(
        policy,
        GovernanceSubject(email="user@example.com", display_name="User", provider="google"),
    )

    assert access.permissions == frozenset({"sessions:read", "chat:use", "logs:read", "mcp:read"})
    assert access.profiles == frozenset({"default", "ops", "custom"})
    assert access.has_permission("chat:use")
    assert access.is_profile_allowed("custom")


def test_sso_group_claim_maps_to_local_group():
    from hermes_cli.dashboard_governance.resolver import GovernanceSubject, resolve_effective_access

    policy = _policy_from_dict(
        {
            "version": 1,
            "mode": "enforce",
            "groups": {
                "sw-eng": {
                    "sso_groups": ["engineering"],
                    "grants": {"permissions": ["tools:read"], "profiles": ["eng-ops"]},
                }
            },
        }
    )
    access = resolve_effective_access(
        policy,
        GovernanceSubject(
            email="eng@example.com",
            display_name="Eng",
            provider="google",
            groups=("engineering",),
        ),
    )

    assert access.has_permission("tools:read")
    assert access.is_profile_allowed("eng-ops")
    assert "group:sw-eng" in access.grant_sources


def test_preview_explains_source_of_grant():
    from hermes_cli.dashboard_governance.resolver import GovernanceSubject, resolve_effective_access

    policy = _policy_from_dict(
        {
            "version": 1,
            "mode": "enforce",
            "roles": {"viewer": {"grants": {"permissions": ["sessions:read"]}}},
            "users": {"user@example.com": {"roles": ["viewer"]}},
        }
    )
    access = resolve_effective_access(
        policy,
        GovernanceSubject(email="user@example.com", display_name="User", provider="google"),
    )

    decision = access.explain_permission("sessions:read")
    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert "role:viewer" in decision.sources
