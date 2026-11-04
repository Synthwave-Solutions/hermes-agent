"""An admin with excluded DWD subjects gets "!a,b" in HERMES_DWD_IDENTITY,
never the str() of the frozenset (03-11-2026: that locked Marcel out of his
own Gmail, Drive and Calendar)."""
from types import SimpleNamespace

from hermes_cli.dashboard_governance import context as gov_context
from hermes_cli.dashboard_governance import tool_policy
from tools.environments import local


def _run(monkeypatch, identity, inherited=None):
    bound = SimpleNamespace(access=SimpleNamespace(mode="enforce"))
    monkeypatch.setattr(gov_context, "current_governance_context", lambda: object())
    monkeypatch.setattr(gov_context, "policy_contexts", lambda ctx: [bound])
    monkeypatch.setattr(tool_policy, "dwd_identity_for", lambda access: identity)
    if inherited is None:
        monkeypatch.delenv("HERMES_DWD_IDENTITY", raising=False)
    else:
        monkeypatch.setenv("HERMES_DWD_IDENTITY", inherited)
    env = {}
    local._inject_dwd_identity_env(env)
    return env.get("HERMES_DWD_IDENTITY")


def test_exclusion_is_encoded_for_hermes_dwd(monkeypatch):
    excl = tool_policy.DwdExclusion({"Michael@Synthwave.Solutions", "b@x.test"})
    assert _run(monkeypatch, excl) == "!b@x.test,michael@synthwave.solutions"


def test_exclusion_survives_a_nested_shell(monkeypatch):
    excl = tool_policy.DwdExclusion({"michael@synthwave.solutions"})
    assert _run(monkeypatch, excl, inherited="!michael@synthwave.solutions") == "!michael@synthwave.solutions"


def test_pinned_identity_unchanged(monkeypatch):
    assert _run(monkeypatch, "alice@example.test") == "alice@example.test"
