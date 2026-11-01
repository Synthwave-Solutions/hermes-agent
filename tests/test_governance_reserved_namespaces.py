"""Reserved permission namespaces in the engine (plan 3.6, 3.11 and 4.3, E3).

org:, billing:, departments:, support:, a2a: and screen: are control-plane
names. A wildcard never reaches them: not in EffectiveAccess.allows, not in
the resolver's level ceiling and not through a whitelist intersection. So an
elevated blacklist account holding the operational "*" (the wildcard-leak
regression subject, power.user@acme.example, as in the shared org_acme
fixture of the WebUI and the cockpit) holds none of them. Inside a namespace
a glob still works ("a2a:call:*"), except screen:, which only an entry naming
the permission exactly grants. Denies stay broad.

The WebUI carries the same rule (W3a); the names and the matcher mirror it.
"""
from __future__ import annotations

import copy

import pytest

from hermes_cli.dashboard_governance import resolver
from hermes_cli.dashboard_governance.context import (
    DashboardGovernanceContext,
    context_from_env_payload,
    serialize_context_for_env,
)
from hermes_cli.dashboard_governance.loader import parse_governance_policy
from hermes_cli.dashboard_governance.models import GrantSet, GovernanceSubject, intersect_grants
from hermes_cli.dashboard_governance.resolver import resolve_effective_access
from hermes_cli.dashboard_governance.route_catalog import RouteRule

# The governance part of the org_acme fixture that matters here: a bootstrap
# admin, a platform admin through access_level admin, and the elevated
# blacklist account.
POLICY = {
    "version": 1,
    "mode": "enforce",
    "default_effect": "deny",
    "bootstrap_admins": ["ops@synthwave.example"],
    "roles": {
        "admin": {"grants": {"permissions": ["governance:read", "governance:write"]}},
        "operator": {"grants": {"permissions": ["chat:use"]}},
        "viewer": {"grants": {"permissions": []}},
    },
    "groups": {
        "administrators": {"roles": ["admin"]},
        "all-staff": {"roles": []},
    },
    "users": {
        "it.admin@acme.example": {"access_level": "admin", "roles": [], "groups": ["all-staff"]},
        "owner@acme.example": {"roles": ["operator"], "groups": ["all-staff"]},
        "power.user@acme.example": {
            "access_level": "elevated",
            "access_mode": "blacklist",
            "roles": ["operator"],
            "groups": ["all-staff"],
        },
    },
}

RESERVED = (
    "org:read",
    "org:members:write",
    "billing:read",
    "departments:read",
    "departments:sales:write",
    "support:tickets",
    "a2a:call:acme-inst_a/sales-bot",
    "a2a:serve:sales-bot",
    "a2a:rpc:groups.peer.invite",
    "a2a:rpc:bot_relay.deliver",
    "screen:watch",
    "screen:takeover",
    "screen:admin",
)
SCREEN = ("screen:watch", "screen:takeover", "screen:admin")


def _policy() -> dict:
    return copy.deepcopy(POLICY)


def _access(raw: dict, email: str):
    return resolve_effective_access(parse_governance_policy(raw), GovernanceSubject(email=email))


def _with_user(raw: dict, email: str, entry: dict) -> dict:
    raw = copy.deepcopy(raw)
    raw["users"][email] = entry
    return raw


# ── The wildcard-leak regression ─────────────────────────────────────────────

def test_elevated_blacklist_user_with_wildcard_gets_no_reserved_permission():
    access = _access(_policy(), "power.user@acme.example")
    assert "*" in access.permissions, "the regression subject must hold the operational wildcard"
    for name in RESERVED:
        assert not access.has_permission(name), name
        assert not access.allows("permissions", name), name
    # Ordinary permissions still follow the wildcard.
    assert access.has_permission("chat:use")
    assert access.has_permission("files:read")
    assert access.has_permission("terminal:use")


def test_bootstrap_and_admin_level_wildcards_do_not_reach_reserved_names():
    raw = _policy()
    for email in ("ops@synthwave.example", "it.admin@acme.example"):
        access = _access(raw, email)
        assert "*" in access.permissions, email
        for name in RESERVED:
            assert not access.has_permission(name), (email, name)
        assert access.has_permission("governance:write"), email


def test_explicit_grant_still_works_outside_the_screen_namespace():
    raw = _with_user(_policy(), "support.lead@acme.example", {
        "roles": ["operator"],
        "grants": {"permissions": ["org:*", "a2a:call:*", "support:tickets"]},
    })
    access = _access(raw, "support.lead@acme.example")
    assert access.has_permission("org:read")
    assert access.has_permission("org:members:write")
    assert access.has_permission("a2a:call:acme-inst_a/sales-bot")
    assert access.has_permission("support:tickets")
    assert not access.has_permission("support:admin")
    assert not access.has_permission("billing:read")
    assert not access.has_permission("a2a:serve:sales-bot")


def test_blacklist_user_keeps_an_explicit_a2a_grant_next_to_the_wildcard():
    raw = _policy()
    raw["users"]["power.user@acme.example"]["grants"] = {"permissions": ["a2a:call:acme-inst_b/*"]}
    access = _access(raw, "power.user@acme.example")
    assert access.has_permission("a2a:call:acme-inst_b/sales")
    assert not access.has_permission("a2a:call:acme-inst_c/sales")
    assert not access.has_permission("a2a:serve:sales")


# ── screen: is exact-name only ───────────────────────────────────────────────

def test_screen_glob_grants_nothing():
    raw = _with_user(_policy(), "screen.glob@acme.example", {
        "roles": ["operator"],
        "grants": {"permissions": ["screen:*", "screen:?atch", "scr*", "*"]},
    })
    access = _access(raw, "screen.glob@acme.example")
    for name in SCREEN:
        assert not access.has_permission(name), name


def test_exact_screen_grant_gives_exactly_that_one():
    raw = _with_user(_policy(), "watcher@acme.example", {
        "roles": ["operator"],
        "grants": {"permissions": ["screen:watch"]},
    })
    access = _access(raw, "watcher@acme.example")
    assert access.has_permission("screen:watch")
    assert not access.has_permission("screen:takeover")
    assert not access.has_permission("screen:admin")


def test_blacklist_user_with_an_exact_screen_grant_gets_only_that_one():
    raw = _policy()
    raw["users"]["power.user@acme.example"]["grants"] = {"permissions": ["screen:takeover"]}
    access = _access(raw, "power.user@acme.example")
    assert access.has_permission("screen:takeover")
    assert not access.has_permission("screen:watch")
    assert not access.has_permission("screen:admin")


# ── Denies stay broad ────────────────────────────────────────────────────────

def test_a_deny_still_removes_an_explicit_reserved_grant():
    raw = _with_user(_policy(), "denied@acme.example", {
        "access_level": "elevated",
        "access_mode": "blacklist",
        "roles": ["operator"],
        "grants": {"permissions": ["screen:watch", "org:read", "a2a:call:acme-inst_b/sales"]},
        "deny": {"permissions": ["screen:*", "org:read", "a2a:*"]},
    })
    access = _access(raw, "denied@acme.example")
    assert not access.has_permission("screen:watch")
    assert not access.has_permission("org:read")
    assert not access.has_permission("a2a:call:acme-inst_b/sales")


# ── Defence in depth: level ceiling and whitelist intersection ───────────────

def test_level_ceiling_never_pulls_a_reserved_catalog_permission_through_a_wildcard(monkeypatch):
    from hermes_cli.dashboard_governance import route_catalog

    extra = (
        RouteRule("/api/test-screen", "screen:watch"),
        RouteRule("/api/test-org", "org:read"),
        RouteRule("/api/test-a2a", "a2a:serve:sales"),
        RouteRule("/api/test-files", "files:read"),
    )
    monkeypatch.setattr(route_catalog, "ROUTE_CATALOG", tuple(route_catalog.ROUTE_CATALOG) + extra)
    ceiling = resolver._level_ceiling(GrantSet(permissions=frozenset({"*"})), "elevated")
    assert "files:read" in ceiling.permissions
    assert "screen:watch" not in ceiling.permissions
    assert "org:read" not in ceiling.permissions
    assert "a2a:serve:sales" not in ceiling.permissions
    explicit = resolver._level_ceiling(
        GrantSet(permissions=frozenset({"*", "screen:watch", "org:*", "a2a:serve:*"})), "elevated")
    assert "screen:watch" in explicit.permissions
    assert "org:read" in explicit.permissions
    assert "a2a:serve:sales" in explicit.permissions


def test_legacy_level_entry_ceiling_keeps_reserved_names_out():
    raw = _with_user(_policy(), "legacy@acme.example", {
        "access_level": "elevated",
        "roles": ["operator"],
        "grants": {"permissions": ["*"]},
    })
    access = _access(raw, "legacy@acme.example")
    assert access.role_ceiling is not None
    assert access.has_permission("chat:use")
    for name in RESERVED:
        assert not access.has_permission(name), name


def test_whitelist_wildcard_does_not_admit_a_reserved_role_grant():
    raw = _policy()
    raw["roles"]["org_reader"] = {"grants": {"permissions": ["chat:use", "org:read", "screen:watch", "a2a:call:x/y"]}}
    raw = _with_user(raw, "listed@acme.example", {
        "access_level": "elevated",
        "access_mode": "whitelist",
        "roles": ["org_reader"],
        "grants": {"permissions": ["*"]},
    })
    access = _access(raw, "listed@acme.example")
    assert access.has_permission("chat:use")
    assert not access.has_permission("org:read")
    assert not access.has_permission("screen:watch")
    assert not access.has_permission("a2a:call:x/y")


def test_whitelist_glob_inside_the_namespace_admits_the_role_grant():
    """A whitelist keeps what both the role (the ceiling) and the person's
    own list grant; a glob inside the a2a: namespace counts on either side."""
    raw = _policy()
    raw["roles"]["caller"] = {"grants": {"permissions": ["chat:use", "a2a:call:acme-inst_b/sales"]}}
    raw = _with_user(raw, "caller@acme.example", {
        "access_level": "elevated",
        "access_mode": "whitelist",
        "roles": ["caller"],
        "grants": {"permissions": ["chat:use", "a2a:call:acme-inst_b/*"]},
    })
    access = _access(raw, "caller@acme.example")
    assert access.has_permission("a2a:call:acme-inst_b/sales")
    assert not access.has_permission("a2a:call:acme-inst_b/finance")


def test_intersection_keeps_a_reserved_name_both_sides_grant():
    left = GrantSet(permissions=frozenset({"org:read", "chat:use"}))
    right = GrantSet(permissions=frozenset({"org:*", "*"}))
    assert intersect_grants(left, right).permissions == frozenset({"org:read", "chat:use"})
    assert intersect_grants(GrantSet(permissions=frozenset({"*"})), GrantSet(
        permissions=frozenset({"org:read", "chat:use"}))).permissions == frozenset({"chat:use"})


def test_a_serialized_turn_context_keeps_the_rule():
    """The WebUI hands the engine a serialized context; the rule holds there."""
    access = _access(_policy(), "power.user@acme.example")
    ctx = DashboardGovernanceContext(subject=access.subject, access=access)
    restored = context_from_env_payload(serialize_context_for_env(ctx))
    assert restored is not None
    assert "*" in restored.access.permissions
    for name in RESERVED:
        assert not restored.access.has_permission(name), name
    assert restored.access.has_permission("chat:use")


# ── The matcher itself ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", RESERVED)
def test_reserved_names_are_recognised(name):
    from hermes_cli.dashboard_governance.models import permission_matches, reserved_namespace

    assert reserved_namespace(name)
    assert not permission_matches({"*"}, name)
    assert not permission_matches({"*:*", "?*", "[a-z]*", "a*", "*call*"}, name)


def test_reserved_detection_ignores_case_so_a_wildcard_cannot_reach_a_cased_name():
    from hermes_cli.dashboard_governance.models import permission_matches, reserved_namespace

    for name in ("ORG:read", "Screen:Watch", "A2A:call:x/y", " a2a:call:x/y"):
        assert reserved_namespace(name)
        assert not permission_matches({"*"}, name)


def test_ordinary_names_keep_the_existing_matcher():
    from hermes_cli.dashboard_governance.models import permission_matches, reserved_namespace

    assert permission_matches({"*"}, "chat:use")
    assert permission_matches({"files:*"}, "files:read")
    assert not permission_matches({"files:*"}, "chat:use")
    # A name that merely contains a reserved word is not reserved.
    assert not reserved_namespace("reports:org:read")
    assert permission_matches({"*"}, "reports:org:read")
