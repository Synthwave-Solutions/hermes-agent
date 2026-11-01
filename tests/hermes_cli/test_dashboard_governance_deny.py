"""Per-user deny (off-toggle) tests: loader parse, GrantSet subtraction and
resolver post-union deny with the bootstrap-admin exemption.

Mirrors the hermes-webui vendored-copy tests (tests/test_governance_deny.py):
the schema and resolution results must stay identical across both apps.
"""
from __future__ import annotations


def _policy_from_dict(data):
    from hermes_cli.dashboard_governance.loader import parse_governance_policy

    return parse_governance_policy(data)


DENY_POLICY = {
    "version": 1,
    "mode": "enforce",
    "default_effect": "deny",
    "bootstrap_admins": ["owner@example.com"],
    "roles": {
        "maker": {
            "grants": {
                "permissions": ["sessions:read", "chat:use"],
                "skills": {"view": ["a", "b", "c"], "load": ["a", "b", "c"]},
                "cli": {"commands": ["git", "ls"]},
                "mcp": {"servers": ["s1", "s2"], "tools": {"s1": ["t1", "t2"]}},
            }
        }
    },
    "groups": {"crew": {"grants": {"skills": {"load": ["d"]}}}},
    "users": {
        "user@example.com": {
            "roles": ["maker"],
            "groups": ["crew"],
            "deny": {
                "permissions": ["chat:use"],
                "skills": {"load": ["b", "d"]},
                "cli": {"commands": ["ls"]},
                "mcp": {"servers": ["s2"], "tools": {"s1": ["t2"]}},
            },
        },
        "owner@example.com": {"deny": {"skills": {"load": ["*"]}}},
    },
}


def test_loader_parses_user_deny():
    policy = _policy_from_dict(DENY_POLICY)
    user = policy.users["user@example.com"]
    assert user.deny.skills_load == frozenset({"b", "d"})
    assert user.deny.cli_commands == frozenset({"ls"})
    assert user.deny.mcp_servers == frozenset({"s2"})
    assert not user.deny.is_empty()


def test_loader_missing_deny_is_empty():
    policy = _policy_from_dict(
        {"version": 1, "mode": "enforce", "users": {"user@example.com": {"roles": ["maker"]}}}
    )
    assert policy.users["user@example.com"].deny.is_empty()


def test_subtract_wildcard_deny_empties_category():
    from hermes_cli.dashboard_governance.models import GrantSet

    base = GrantSet.from_mapping({"skills": {"load": ["a", "b"], "view": ["a", "b"]}})
    deny = GrantSet.from_mapping({"skills": {"load": ["*"]}})
    out = base.subtract(deny)
    assert out.skills_load == frozenset()
    assert out.skills_view == frozenset({"a", "b"})


def test_subtract_specific_deny_cannot_narrow_wildcard_allow():
    # Documented limitation: "*" stays "*" under a specific deny; denies are
    # only meaningful on explicit whitelists.
    from hermes_cli.dashboard_governance.models import GrantSet

    base = GrantSet.from_mapping({"skills": {"load": ["*"]}})
    deny = GrantSet.from_mapping({"skills": {"load": ["a"]}})
    assert base.subtract(deny).skills_load == frozenset({"*"})


def test_subtract_never_touches_denied_globs_or_caps():
    from hermes_cli.dashboard_governance.models import GrantSet

    base = GrantSet.from_mapping(
        {"files": {"denied_globs": ["**/.env"]}, "usage_caps": {"daily_tokens": 5}}
    )
    deny = GrantSet.from_mapping(
        {"files": {"denied_globs": ["**/.env"]}, "usage_caps": {"daily_tokens": 99}}
    )
    out = base.subtract(deny)
    assert out.file_denied_globs == frozenset({"**/.env"})
    assert out.usage_caps == {"daily_tokens": 5}


def test_resolver_subtracts_deny_after_union():
    from hermes_cli.dashboard_governance.resolver import GovernanceSubject, resolve_effective_access

    policy = _policy_from_dict(DENY_POLICY)
    access = resolve_effective_access(policy, GovernanceSubject(email="user@example.com"))

    # deny wins over role AND group grants
    assert access.grants.skills_load == frozenset({"a", "c"})
    assert access.grants.skills_view == frozenset({"a", "b", "c"})
    assert access.grants.cli_commands == frozenset({"git"})
    assert access.grants.mcp_servers == frozenset({"s1"})
    assert access.grants.mcp_tools == {"s1": frozenset({"t1"})}
    assert not access.has_permission("chat:use")
    assert access.has_permission("sessions:read")
    assert "deny:user:user@example.com" in access.grant_sources
    assert "chat:use" not in access.permission_sources


def test_resolver_bootstrap_admin_exempt_from_deny():
    from hermes_cli.dashboard_governance.resolver import GovernanceSubject, resolve_effective_access

    policy = _policy_from_dict(DENY_POLICY)
    access = resolve_effective_access(policy, GovernanceSubject(email="owner@example.com"))
    # a stray deny: "*" must not brick the owner
    assert "*" in access.grants.skills_load
    assert "deny:user:owner@example.com" not in access.grant_sources


def test_resolver_empty_deny_leaves_sources_untouched():
    from hermes_cli.dashboard_governance.resolver import GovernanceSubject, resolve_effective_access

    policy = _policy_from_dict(
        {
            "version": 1,
            "mode": "enforce",
            "users": {"user@example.com": {"grants": {"permissions": ["sessions:read"]}}},
        }
    )
    access = resolve_effective_access(policy, GovernanceSubject(email="user@example.com"))
    assert not any(source.startswith("deny:") for source in access.grant_sources)


# ── The ask behind an access request (27 Aug 2026 ticket) ───────────────────
# An admin deciding on a request needs the user's own words, redacted and
# truncated before they are ever stored.

class TestTriggerRedaction:
    def test_credential_shaped_runs_are_masked(self):
        from hermes_cli.dashboard_governance.grant_requests import redact_trigger
        for secret, probe in [
            ("sk-abcdef1234567890abcdef", "sk-abcdef"),
            ("ghp_ABCDEFGHIJKLMNOPQRSTUV", "ghp_ABCD"),
            ("api_key: hunter2superlong", "hunter2superlong"),
            ("password=Zomer2026!", "Zomer2026!"),
            ("deadbeefdeadbeefdeadbeefdeadbeef", "deadbeef"),
        ]:
            out = redact_trigger(f"please use {secret} for me")
            assert probe not in out, out
            assert "[REDACTED]" in out

    def test_ordinary_text_survives_intact(self):
        from hermes_cli.dashboard_governance.grant_requests import redact_trigger
        text = "pull my open github issues and put them on my todo list"
        assert redact_trigger(f"  {text}  ") == text

    def test_long_input_is_truncated_with_an_ellipsis(self):
        from hermes_cli.dashboard_governance.grant_requests import redact_trigger
        out = redact_trigger("x" * 900)
        assert len(out) <= 400 and out.endswith("…")

    def test_empty_input_stays_empty_never_a_placeholder(self):
        from hermes_cli.dashboard_governance.grant_requests import redact_trigger
        assert redact_trigger("") == "" and redact_trigger(None) == ""

    def test_denial_stores_the_first_trigger_not_the_latest(self, tmp_path, monkeypatch):
        from hermes_cli.dashboard_governance import grant_requests as gr
        monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))

        class _Subject:
            email = "steve@example.test"

        class _Ctx:
            access = type("A", (), {"subject": _Subject()})()

        monkeypatch.setenv("HERMES_SESSION_LAST_USER_MESSAGE", "get my github issues")
        assert gr.record_denial(_Ctx(), "load_skill", "skill_not_allowed", "my-day")
        monkeypatch.setenv("HERMES_SESSION_LAST_USER_MESSAGE", "retrying the same thing")
        assert gr.record_denial(_Ctx(), "load_skill", "skill_not_allowed", "my-day")
        entry = next(iter(gr.load_store().values()))
        assert entry["trigger"] == "get my github issues"
        assert entry["count"] == 2

    def test_missing_env_stores_no_trigger(self, tmp_path, monkeypatch):
        from hermes_cli.dashboard_governance import grant_requests as gr
        monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(tmp_path))
        monkeypatch.delenv("HERMES_SESSION_LAST_USER_MESSAGE", raising=False)

        class _Subject:
            email = "steve@example.test"

        class _Ctx:
            access = type("A", (), {"subject": _Subject()})()

        assert gr.record_denial(_Ctx(), "load_skill", "skill_not_allowed", "other")
        assert next(iter(gr.load_store().values()))["trigger"] == ""
