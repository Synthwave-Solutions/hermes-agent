"""Native schema/dispatch paths with live file-backed membership callbacks."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Barrier

import pytest

import model_tools
from hermes_cli.dashboard_governance import context as context_module
from hermes_cli.dashboard_governance.context import (
    DashboardGovernanceContext, context_from_env_payload, governance_context,
    serialize_context_for_env,
)
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from hermes_cli.dashboard_governance.tool_policy import (
    tool_allowed_for_context, tool_arguments_allowed_for_context,
)


NAMES = tuple(f"qa_projection_{n}" for n in range(40))
TOOLSET = "qa-governance-projection"


@pytest.fixture
def catalog():
    registry = model_tools.registry
    for name in NAMES:
        registry.register(name, TOOLSET, {"name": name, "parameters": {"type": "object"}},
                          lambda args, **kwargs: {"fixture": True})
    model_tools._clear_tool_defs_cache()
    yield registry.get_definitions(set(NAMES), quiet=True)
    for name in NAMES:
        registry.deregister(name)
    model_tools._clear_tool_defs_cache()


def context(*, actor="alice@example.test", profile="default", grants=None, deny=None, mode="enforce"):
    subject = GovernanceSubject(email=actor)
    access = EffectiveAccess(
        subject=subject, mode=mode, roles=frozenset({"member"}),
        access_mode="blacklist", access_level="elevated", profiles=frozenset({"*"}),
        permissions=frozenset({"*"}),
        grants=grants or GrantSet(tools=frozenset({"*"}), file_read_roots=frozenset({"*"}),
                                 cli_commands=frozenset({"*"}), cli_workdir_roots=frozenset({"*"})),
        deny=deny or GrantSet(),
    )
    return DashboardGovernanceContext(subject=subject, access=access, active_profile=profile)


class Membership:
    def __init__(self, path, actor="alice@example.test", profile="default"):
        self.path, self.actor, self.profile = path, actor, profile
        self.root = str(path.parent / f"workspace-{profile}")
        self.reads = 0
        self.write(True)

    def write(self, allowed, version=1):
        self.path.write_text(json.dumps({"version": version, "actor": self.actor,
                                        "profile": self.profile, "root": self.root, "allowed": allowed}))

    def check(self, root):
        self.reads += 1
        policy = json.loads(self.path.read_text())
        return (policy["allowed"] is True and policy["actor"] == self.actor
                and policy["profile"] == self.profile and policy["root"] == root)

    def context(self, **kwargs):
        return replace(context(actor=self.actor, profile=self.profile, **kwargs),
                       workspace_path=self.root, workspace_access_check=self.check)


def project(ctx, catalog):
    with governance_context(ctx):
        return model_tools._filter_tools_by_governance(catalog)


def construct(ctx):
    with governance_context(ctx):
        return model_tools.get_tool_definitions([TOOLSET], quiet_mode=True, skip_tool_search_assembly=True)


def test_live_scope_reads_are_bounded_by_projection_not_tool_count(tmp_path, catalog):
    membership = Membership(tmp_path / "acl.json")
    projected = project(membership.context(), catalog)
    assert projected == catalog
    assert all(left is right for left, right in zip(projected, catalog))
    assert membership.reads == 2


def test_continuation_is_decoded_once_and_each_live_ceiling_is_rechecked(tmp_path, catalog, monkeypatch):
    membership = Membership(tmp_path / "acl.json")
    original = membership.context(deny=GrantSet(tools=frozenset({NAMES[0]})))
    current = replace(membership.context(), continuation_contexts=(serialize_context_for_env(original),))
    decode = context_module.context_from_env_payload
    calls = []

    def counted(payload):
        calls.append(1)
        return decode(payload)

    monkeypatch.setattr(context_module, "context_from_env_payload", counted)
    assert {t["function"]["name"] for t in project(current, catalog)} == set(NAMES) - {NAMES[0]}
    assert len(calls) == 1
    assert membership.reads == 4  # current + retained ceiling, before + after


def test_revocation_during_projection_closes_the_entire_result(tmp_path, catalog, monkeypatch):
    membership = Membership(tmp_path / "acl.json")
    native = model_tools.registry.get_entry
    invoked = False

    def revoke(name, *args, **kwargs):
        nonlocal invoked
        if not invoked:
            invoked = True
            membership.write(False, version=2)
        return native(name, *args, **kwargs)

    monkeypatch.setattr(model_tools.registry, "get_entry", revoke)
    assert project(membership.context(), catalog) == []
    assert invoked


def test_cache_hit_observes_membership_revocation_without_poisoning_schema_cache(tmp_path, catalog):
    membership = Membership(tmp_path / "acl.json")
    ctx = membership.context()
    initial = construct(ctx)
    assert len(initial) == len(catalog)
    assert model_tools._tool_defs_cache  # exercise a real warm constructor path
    membership.write(False, version=2)
    assert construct(ctx) == []
    assert model_tools._last_resolved_tool_names == []
    membership.write(True, version=3)
    assert construct(ctx) == initial
    assert set(model_tools._last_resolved_tool_names) == set(NAMES)


@pytest.mark.parametrize("during_projection", [False, True])
def test_initial_scope_denial_does_not_cache_an_empty_catalog(tmp_path, catalog, monkeypatch, during_projection):
    membership = Membership(tmp_path / "acl.json")
    ctx = membership.context()
    if during_projection:
        native = model_tools.registry.get_entry

        def revoke(name, *args, **kwargs):
            membership.write(False, version=2)
            return native(name, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(model_tools.registry, "get_entry", revoke)
            assert construct(ctx) == []
    else:
        membership.write(False)
        assert construct(ctx) == []
    membership.write(True, version=3)
    assert len(construct(ctx)) == len(catalog)


def test_concurrent_cache_population_cannot_restore_revoked_schemas(tmp_path, catalog, monkeypatch):
    membership = Membership(tmp_path / "acl.json")
    ctx = membership.context()
    initial = construct(ctx)
    saved_cache = dict(model_tools._tool_defs_cache)
    model_tools._clear_tool_defs_cache()
    native = model_tools._compute_tool_definitions

    def compute(*args, **kwargs):
        result = native(*args, **kwargs)
        # Deterministic boundary: another constructor inserted its older result.
        model_tools._tool_defs_cache.update(saved_cache)
        membership.write(False, version=2)
        return result

    monkeypatch.setattr(model_tools, "_compute_tool_definitions", compute)
    assert construct(ctx) == []
    assert model_tools._last_resolved_tool_names == []
    assert next(iter(model_tools._tool_defs_cache.values())) == initial


def test_policy_version_change_uses_new_grants_without_a_projection_cache(tmp_path, catalog):
    from hermes_cli.dashboard_governance.loader import load_governance_policy
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access

    path = tmp_path / "governance.yaml"
    subject = GovernanceSubject(email="alice@example.test")

    def fresh(version, name):
        path.write_text(json.dumps({"version": version, "mode": "enforce",
            "roles": {"technical": {"grants": {"tools": {"builtins": ["*"]}}}}, "users": {
            subject.email: {"roles": ["technical"], "access_mode": "whitelist", "access_level": "elevated",
                            "grants": {"tools": {"builtins": [name]}}}}}))
        access = resolve_effective_access(load_governance_policy(path=path), subject)
        return DashboardGovernanceContext(subject=subject, access=access, active_profile="default")

    first = construct(fresh(1, NAMES[0]))
    second = construct(fresh(2, NAMES[1]))
    assert [td["function"]["name"] for td in first] == [NAMES[0]]
    assert [td["function"]["name"] for td in second] == [NAMES[1]]


def test_bot_membership_checks_are_local_and_revalidated_on_dispatch(catalog):
    state = {"allowed": True, "checks": 0}

    def check():
        state["checks"] += 1
        return state["allowed"]

    ctx = context()
    ctx = replace(ctx, bot_access_ceiling=ctx.access, bot_access_check=check)
    assert project(ctx, catalog) == catalog
    assert state["checks"] == 2
    state["allowed"] = False
    assert tool_allowed_for_context(ctx, NAMES[0], model_tools.registry).reason == "bot_access_revoked"
    assert state["checks"] == 3


@pytest.mark.parametrize("checker", [None, "raises", "wrong_result"])
def test_invalid_bot_membership_callback_fails_closed(catalog, checker):
    def fail():
        raise ValueError("fixture membership unavailable")

    callback = fail if checker == "raises" else (lambda: 1) if checker == "wrong_result" else None
    ctx = context()
    assert project(replace(ctx, bot_access_ceiling=ctx.access, bot_access_check=callback), catalog) == []


@pytest.mark.parametrize("mode", ["enforce", "off", "report_only"])
def test_dispatch_and_argument_checks_remain_live_after_projection(tmp_path, catalog, monkeypatch, mode):
    membership = Membership(tmp_path / "acl.json")
    ctx = membership.context(mode=mode)
    assert project(ctx, catalog) == catalog
    membership.write(False, version=2)
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda *a, **k: pytest.fail("revoked handler invoked"))
    with governance_context(ctx):
        result = json.loads(model_tools.handle_function_call(
            NAMES[0], {}, skip_pre_tool_call_hook=True, skip_tool_execution_middleware=True))
    assert result["governance"]["reason"] == "workspace_access_revoked"
    assert not tool_arguments_allowed_for_context(ctx, "read_file", {"path": membership.root}).allowed


def test_delegated_envelope_never_inherits_projection_authority(tmp_path, catalog):
    membership = Membership(tmp_path / "acl.json")
    ctx = membership.context()
    assert project(ctx, catalog) == catalog
    serialized_child = context_from_env_payload(serialize_context_for_env(ctx))
    assert project(serialized_child, catalog) == []  # no trusted callback survives serialization
    current_child = replace(ctx, continuation_contexts=(serialize_context_for_env(ctx),))
    membership.write(False, version=2)
    assert not tool_allowed_for_context(current_child, NAMES[0], model_tools.registry).allowed


@pytest.mark.parametrize("failure", ["invalid_json", "missing_store", "missing_callback", "wrong_result"])
def test_malformed_or_unavailable_scope_fails_closed(tmp_path, catalog, failure):
    membership = Membership(tmp_path / "acl.json")
    ctx = membership.context()
    assert construct(ctx)
    if failure == "invalid_json":
        membership.path.write_text("{")
    elif failure == "missing_store":
        membership.path.unlink()
    elif failure == "missing_callback":
        ctx = replace(ctx, workspace_access_check=None)
    else:
        ctx = replace(ctx, workspace_access_check=lambda _: 1)
    assert project(ctx, catalog) == []
    assert construct(ctx) == []


@pytest.mark.parametrize("continuation", ["malformed", "other_actor", "other_profile"])
def test_invalid_continuation_cannot_project_tools(catalog, continuation):
    original = context(actor="other@example.test") if continuation == "other_actor" else context(profile="other")
    payload = "invalid" if continuation == "malformed" else serialize_context_for_env(original)
    assert project(replace(context(), continuation_contexts=(payload,)), catalog) == []


@pytest.mark.parametrize("restriction", ["whitelist", "deny", "resource_deny", "user_role", "bot_cli", "bot_revoke"])
def test_projection_matches_native_per_tool_policy_and_python_barrier(catalog, restriction):
    ctx = context()
    if restriction == "whitelist":
        ctx = replace(ctx, access=replace(ctx.access, access_mode="whitelist", grants=GrantSet(tools=frozenset({NAMES[0]}))))
    elif restriction == "deny":
        ctx = replace(ctx, access=replace(ctx.access, deny=GrantSet(tools=frozenset({NAMES[0]}))))
    elif restriction == "resource_deny":
        ctx = replace(ctx, access=replace(ctx.access, deny=GrantSet(file_read_roots=frozenset({"/fixture/bunq"}))))
    elif restriction == "user_role":
        ctx = replace(ctx, access=replace(ctx.access, access_level="user"))
    elif restriction == "bot_cli":
        ceiling = replace(ctx.access, grants=replace(ctx.access.grants, cli_commands=frozenset()))
        ctx = replace(ctx, bot_access_ceiling=ceiling, bot_access_check=lambda: True)
    else:
        ctx = replace(ctx, bot_access_ceiling=ctx.access, bot_access_check=lambda: False)
    schemas = catalog + [{"type": "function", "function": {"name": name}}
                         for name in ("terminal", "execute_code", "read_file")]
    expected = [td for td in schemas if tool_allowed_for_context(ctx, td["function"]["name"], model_tools.registry).allowed]
    assert project(ctx, schemas) == expected
    if restriction in {"resource_deny", "user_role"}:
        assert not {"terminal", "execute_code"} & {td["function"]["name"] for td in expected}


def test_parallel_actors_profiles_and_roots_have_no_shared_projection_state(tmp_path, catalog):
    barrier = Barrier(2)

    def worker(actor, profile, allowed):
        membership = Membership(tmp_path / f"{profile}.json", actor, profile)
        membership.write(allowed)
        entered = False

        def check(path):
            nonlocal entered
            if not entered:
                entered = True
                barrier.wait(timeout=10)
            return membership.check(path)

        ctx = replace(membership.context(), workspace_access_check=check)
        return project(ctx, catalog)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, "alice@example.test", "alpha", True)
        second = pool.submit(worker, "bob@example.test", "beta", False)
        assert first.result(timeout=15) == catalog
        assert second.result(timeout=15) == []
