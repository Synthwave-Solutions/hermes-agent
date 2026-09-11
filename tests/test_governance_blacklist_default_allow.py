"""Blacklists allow operational resources by default, without changing identities."""
from copy import deepcopy
import pytest
from hermes_cli.dashboard_governance.loader import parse_governance_policy
from hermes_cli.dashboard_governance.models import GovernanceSubject
from hermes_cli.dashboard_governance.resolver import resolve_effective_access

EMAIL = "technical@example.test"

def policy(mode="blacklist", level="elevated"):
    user = {"roles": ["narrow"], "access_mode": mode, "access_level": level,
            "grants": {"profiles": ["historical"], "env": {"vars": ["TECH_KEY"]},
                       "files": {"denied_globs": ["*/private/*"], "allow_globs": ["*/private/example.md"]},
                       "cli": {"approval_commands": ["deploy-prod"]}, "usage_caps": {"tool_calls": 12}},
            "approval": {"mode": "automatic", "prompt": "Approve ordinary engineering; deny banking."}}
    if not mode: user.pop("access_mode")
    if not level: user.pop("access_level")
    return {"mode": "enforce", "roles": {"narrow": {"grants": {
        "permissions": ["chat:use"], "profiles": ["old"], "routes": ["/api/chat"],
        "tools": {"builtins": ["web_search"]}, "files": {"denied_globs": ["*/bank/*"]},
    }}}, "users": {EMAIL: user}}

def access(raw):
    return resolve_effective_access(parse_governance_policy(raw), GovernanceSubject(email=EMAIL))

@pytest.mark.parametrize("dimension,value", [
    ("permissions", "future_technical_feature:write"), ("profiles", "default"),
    ("profiles", "historical"), ("profiles", "new-technical-profile"),
    ("routes", "/api/new-technical-tool"), ("settings_write", "theme"),
    ("tools", "read_file"), ("toolsets", "new-tools"), ("skills_manage", "new-skill"),
    ("skills_load", "new-skill"), ("mcp_servers", "github"),
    ("models", "new-model"), ("model_providers", "new-provider"),
    ("file_read_roots", "/new/project/readme.md"), ("file_write_roots", "/new/project/code.py"),
    ("cli_commands", "git"), ("cli_workdir_roots", "/new/project"), ("workspaces", "Aligned"),
])
def test_blacklist_allows_resources_absent_from_role_and_direct_grants(dimension, value):
    a = access(policy())
    assert a.allows(dimension, value)
    assert a.role_ceiling is None

@pytest.mark.parametrize("level", ["", "user", "elevated", "admin"])
def test_levels_and_explicit_denials_remain_independent(level):
    raw = policy(level=level)
    raw["users"][EMAIL]["deny"] = {"tools": {"builtins": ["*bank*"]}, "profiles": ["private-*"],
        "permissions": ["dangerous:write"], "routes": ["/api/private/*"]}
    a = access(raw)
    assert not a.is_tool_allowed("bank_transfer")
    assert not a.is_profile_allowed("private-person")
    assert not a.has_permission("dangerous:write")
    assert not a.is_route_allowed("/api/private/accounts")
    assert a.has_permission("governance:write") == (level == "admin")
    assert a.has_permission("terminal:use") == (level in {"admin", "elevated"})
    assert a.has_permission("profiles:admin") == (level in {"admin", "elevated"})
    assert bool(a.roles & {"owner", "admin"}) == (level == "admin")


def test_default_allow_preserves_constraints_and_explicit_secret_forwarding():
    a = access(policy())
    assert a.grants.file_denied_globs == frozenset({"*/bank/*", "*/private/*"})
    assert a.grants.file_allow_globs == frozenset({"*/private/example.md"})
    assert a.grants.cli_approval_commands == frozenset({"deploy-prod"})
    assert a.grants.usage_caps == {"tool_calls": 12}
    assert a.allows("env_vars", "TECH_KEY")
    assert not a.allows("env_vars", "OTHER_SECRET")
    assert a.approval_mode == "automatic" and a.approval_configured
    assert a.approval_prompt == policy()["users"][EMAIL]["approval"]["prompt"]

@pytest.mark.parametrize("mode,level", [("whitelist", "elevated"), ("", "elevated"), ("", "")])
def test_legacy_and_whitelist_do_not_gain_unconfigured_resources(mode, level):
    a = access(policy(mode, level))
    assert not a.is_tool_allowed("unlisted_tool")
    assert not a.is_profile_allowed("new-technical-profile")
    assert not a.has_permission("future_technical_feature:write")
    assert a.is_profile_allowed("historical") == (mode != "whitelist")


def test_bootstrap_admin_exemption_is_preserved():
    raw = policy(level="user")
    raw["bootstrap_admins"] = [EMAIL]
    raw["users"][EMAIL]["deny"] = {"permissions": ["*"], "profiles": ["*"]}
    a = access(raw)
    assert a.has_permission("governance:write")
    assert a.has_permission("terminal:use")
    assert a.is_profile_allowed("default")


def test_resolver_does_not_rewrite_other_users_or_the_input_policy():
    raw = policy()
    raw["users"]["other@example.test"] = {"roles": ["narrow"]}
    before = deepcopy(raw)
    access(raw)
    assert raw == before
    other = resolve_effective_access(parse_governance_policy(raw), GovernanceSubject(email="other@example.test"))
    assert not other.is_tool_allowed("unlisted_tool")

@pytest.mark.parametrize("mode", ["blacklist", "whitelist", ""])
def test_technical_storage_exception_cannot_override_specific_blacklist_denial(mode):
    raw = policy(mode, "elevated")
    files = {"denied_globs": ["**/.hermes/**", "**/.env", "*bunq*", "*/credentials/*"],
             "allow_globs": ["/home/test/.hermes/profiles/technical/workspace/**"]}
    raw["roles"]["narrow"]["grants"]["files"] = files
    raw["users"][EMAIL]["grants"]["files"] = files
    a = access(raw)
    root = "/home/test/.hermes/profiles/technical/workspace/"
    assert not a.configuration_denies_file(root + "source.py")
    for suffix in (".env", "bunq.json", "credentials/token.json"):
        assert a.configuration_denies_file(root + suffix) == (mode == "blacklist")
    assert a.configuration_denies_file("/home/test/.hermes/unassigned/source.py")
