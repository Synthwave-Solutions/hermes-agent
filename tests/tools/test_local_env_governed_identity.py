"""A governed session's identity and bot ceiling reach every terminal child.

``tools/environments/local.py`` builds a child environment on two paths: the
foreground terminal (``_make_run_env``, used by ``LocalEnvironment.execute``)
and the background and PTY path (``_sanitize_subprocess_env``). The cron
rules of a governed person are enforced in the child by ``cron.jobs``, which
can only see what the environment carries:

- ``HERMES_DWD_IDENTITY``: the address of a governed non-admin under enforce.
  Only the background path used to set it, so ``hermes cron edit`` in the
  normal (foreground) terminal acted as an ungoverned caller.
- ``HERMES_DWD_IDENTITY_BOT_CEILING``: the address of a session a bot access
  ceiling narrows under enforce. No path handed the ceiling down, so a ceiled
  session's terminal could change and run jobs the session itself may not.

Both values are decided by the process's own inherited value and the bound
governance context, never by the env dict a caller passes in: an inherited
value is never dropped, and a bound context that names another address makes
the child match no account at all.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from tools.environments import local

IDENTITY = "HERMES_DWD_IDENTITY"
CEILING = "HERMES_DWD_IDENTITY_BOT_CEILING"
UNRESOLVED = "unresolved-identity"
MALLORY = "mallory@example.test"
ALICE = "alice@example.test"
ADMIN = "root@example.test"


def _ctx(email, *, admin=False, mode="enforce", ceiling=False):
    access = EffectiveAccess(
        subject=GovernanceSubject(email=email),
        mode=mode,
        roles=frozenset({"owner", "admin"} if admin else {"tech_lead"}),
        grants=GrantSet(tools=frozenset({"terminal", "cronjob"}), cli_commands=frozenset({"hermes"})),
        grant_sources=("bootstrap_admin",) if admin else (),
    )
    ctx = DashboardGovernanceContext(subject=access.subject, access=access)
    if ceiling:
        bot = EffectiveAccess(
            subject=GovernanceSubject(email="bot@example.test"),
            mode="enforce",
            roles=frozenset({"bot"}),
            grants=GrantSet(tools=frozenset({"terminal"}), cli_commands=frozenset({"hermes"})),
        )
        ctx = replace(ctx, bot_access_ceiling=bot, bot_access_check=lambda: True)
    return ctx


@pytest.fixture(autouse=True)
def _no_inherited_governance(monkeypatch):
    """Start every test from an ungoverned process."""
    for name in (IDENTITY, CEILING, "HERMES_DASHBOARD_GOVERNANCE_CONTEXT"):
        monkeypatch.delenv(name, raising=False)


def _foreground(env=None):
    return local._make_run_env(dict(env or {}))


def _background(base=None, extra=None):
    return local._sanitize_subprocess_env(dict(os.environ) if base is None else base, extra)


PATHS = [pytest.param(_foreground, id="foreground"), pytest.param(_background, id="background")]


# ---------------------------------------------------------------------------
# HERMES_DWD_IDENTITY
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", PATHS)
def test_a_governed_non_admin_child_carries_the_identity(build):
    with governance_context(_ctx(MALLORY)):
        env = build()
    assert env[IDENTITY] == MALLORY


@pytest.mark.parametrize("build", PATHS)
@pytest.mark.parametrize(
    "ctx",
    [
        pytest.param(None, id="ungoverned"),
        pytest.param(_ctx(ADMIN, admin=True), id="admin"),
        pytest.param(_ctx(MALLORY, mode="report_only"), id="report_only"),
        pytest.param(_ctx(MALLORY, mode="off"), id="off"),
    ],
)
def test_admins_ungoverned_and_non_enforce_children_get_no_identity(build, ctx):
    with governance_context(ctx):
        env = build()
    assert IDENTITY not in env
    assert CEILING not in env


@pytest.mark.parametrize("build", PATHS)
def test_an_inherited_identity_survives_a_process_with_no_bound_context(build, monkeypatch):
    """A nested process started from a governed shell (for example a nested
    ``hermes chat``) binds no context; its children stay that person's."""
    monkeypatch.setenv(IDENTITY, MALLORY)
    assert build()[IDENTITY] == MALLORY


@pytest.mark.parametrize("build", PATHS)
def test_an_inherited_identity_survives_an_administrators_context(build, monkeypatch):
    monkeypatch.setenv(IDENTITY, MALLORY)
    with governance_context(_ctx(ADMIN, admin=True)):
        assert build()[IDENTITY] == MALLORY


@pytest.mark.parametrize("build", PATHS)
def test_a_bound_context_that_names_another_address_matches_no_account(build, monkeypatch):
    monkeypatch.setenv(IDENTITY, MALLORY)
    with governance_context(_ctx(ALICE)):
        assert build()[IDENTITY] == UNRESOLVED


@pytest.mark.parametrize("build", PATHS)
def test_a_bound_context_for_the_same_address_keeps_it(build, monkeypatch):
    monkeypatch.setenv(IDENTITY, MALLORY.upper())
    with governance_context(_ctx(MALLORY)):
        assert build()[IDENTITY] == MALLORY


def test_an_env_dict_value_never_sets_or_replaces_the_identity(monkeypatch):
    """Only the process's own identity and its bound context decide it."""
    assert IDENTITY not in _foreground({IDENTITY: ADMIN})
    assert IDENTITY not in _background({"PATH": "/usr/bin"}, {IDENTITY: ADMIN})
    monkeypatch.setenv(IDENTITY, MALLORY)
    assert _foreground({IDENTITY: ADMIN})[IDENTITY] == MALLORY
    assert _background({"PATH": "/usr/bin", IDENTITY: ADMIN}, {IDENTITY: ADMIN})[IDENTITY] == MALLORY


@pytest.mark.parametrize("build", PATHS)
def test_an_unreadable_context_fails_closed(build):
    broken = replace(_ctx(ADMIN, admin=True), continuation_contexts=("not a context",))
    with governance_context(broken):
        env = build()
    assert env[IDENTITY] == UNRESOLVED
    assert env[CEILING] == UNRESOLVED


# ---------------------------------------------------------------------------
# The bot ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build", PATHS)
@pytest.mark.parametrize(
    "email, admin, identity",
    [
        pytest.param(MALLORY, False, MALLORY, id="non-admin"),
        pytest.param(ADMIN, True, None, id="admin"),
    ],
)
def test_a_ceiled_sessions_child_carries_the_ceiling(build, email, admin, identity):
    """The WebUI bot mode binds the ceiling in-process only; the child gets
    the ceiled session's own address, admin or not."""
    with governance_context(_ctx(email, admin=admin, ceiling=True)):
        env = build()
    assert env[CEILING] == email
    assert env.get(IDENTITY) == identity


@pytest.mark.parametrize("build", PATHS)
def test_a_ceiling_outside_enforce_is_not_handed_down(build):
    with governance_context(_ctx(ADMIN, admin=True, mode="report_only", ceiling=True)):
        assert CEILING not in build()


@pytest.mark.parametrize("build", PATHS)
def test_an_inherited_ceiling_survives(build, monkeypatch):
    monkeypatch.setenv(CEILING, ADMIN)
    assert build()[CEILING] == ADMIN
    with governance_context(_ctx(ADMIN, admin=True)):
        assert build()[CEILING] == ADMIN
    with governance_context(_ctx(ADMIN, admin=True, ceiling=True)):
        assert build()[CEILING] == ADMIN
    with governance_context(_ctx(MALLORY, ceiling=True)):
        assert build()[CEILING] == UNRESOLVED


@pytest.mark.parametrize("build", PATHS)
def test_an_inherited_empty_ceiling_stays_a_ceiling(build, monkeypatch):
    monkeypatch.setenv(CEILING, "")
    assert build()[CEILING] == UNRESOLVED


def test_an_env_dict_value_never_sets_the_ceiling():
    assert CEILING not in _foreground({CEILING: ADMIN})
    assert CEILING not in _background({"PATH": "/usr/bin", CEILING: ADMIN}, {CEILING: ADMIN})


def test_cron_reads_the_variable_the_spawn_paths_write():
    import cron.jobs as jobs

    assert jobs._BOT_CEILING_IDENTITY_ENV == local._BOT_CEILING_IDENTITY_ENV == CEILING
    assert jobs._GOVERNED_SHELL_IDENTITY_ENV == IDENTITY


# ---------------------------------------------------------------------------
# The real foreground terminal
# ---------------------------------------------------------------------------


def _echo(env, name):
    return env.execute(f"echo ${{{name}-unset}}", timeout=60)["output"].strip()


def test_the_foreground_terminal_carries_the_identity(tmp_path):
    with governance_context(_ctx(MALLORY)):
        env = local.LocalEnvironment(cwd=str(tmp_path), timeout=60)
        try:
            assert _echo(env, IDENTITY) == MALLORY
            assert _echo(env, CEILING) == "unset"
        finally:
            env.cleanup()


def test_the_foreground_terminal_carries_the_ceiling(tmp_path):
    with governance_context(_ctx(ADMIN, admin=True, ceiling=True)):
        env = local.LocalEnvironment(cwd=str(tmp_path), timeout=60)
        try:
            assert _echo(env, CEILING) == ADMIN
            assert _echo(env, IDENTITY) == "unset"
        finally:
            env.cleanup()


def test_the_foreground_terminal_keeps_an_inherited_identity(tmp_path, monkeypatch):
    monkeypatch.setenv(IDENTITY, MALLORY)
    env = local.LocalEnvironment(cwd=str(tmp_path), timeout=60)
    try:
        assert _echo(env, IDENTITY) == MALLORY
    finally:
        env.cleanup()


# ---------------------------------------------------------------------------
# The command gate refuses a command that names either variable
#
# The ceiling variable's name contains HERMES_DWD_IDENTITY, so the gate's
# existing tamper guard (tool_policy._check_identity_env_tamper) covers it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "env -u HERMES_DWD_IDENTITY_BOT_CEILING hermes cron edit job1 --prompt x",
        "HERMES_DWD_IDENTITY_BOT_CEILING= hermes cron notepad job1 set task x",
        "unset HERMES_DWD_IDENTITY_BOT_CEILING",
        "env -u HERMES_DWD_IDENTITY hermes cron edit job1 --prompt x",
    ],
)
@pytest.mark.parametrize("ceiling", [False, True], ids=["plain", "ceiled"])
def test_the_command_gate_refuses_naming_the_variables(command, ceiling):
    from hermes_cli.dashboard_governance.tool_policy import tool_arguments_allowed_for_context

    decision = tool_arguments_allowed_for_context(_ctx(MALLORY, ceiling=ceiling), "terminal", {"command": command})
    assert not decision.allowed
    assert decision.reason == "dwd_identity_tamper"
