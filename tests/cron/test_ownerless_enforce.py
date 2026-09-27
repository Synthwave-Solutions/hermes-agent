"""Under governance ``enforce`` an agent job must have an owner to run.

``_governed_as_job_owner`` binds the owner's governance for every fire. A job
without an owner used to run with no governance at all, i.e. with the full
rights of the platform owner. Under ``enforce`` such an agent job is now
refused and the refusal is recorded on the job, its execution and the
incident store. Jobs owned by the configured system principal
(``cron.system_principal``) run under that principal's governance like any
other owner. Jobs made at the CLI, or by a governed administrator, are stamped
with the system principal so they do not end up ownerless; a job made in a
governed person's shell is theirs and never the principal's.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

import cron.scheduler as s

PRINCIPAL = "cron-system@example.test"


def _home():
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _write_policy(mode="enforce", *, bootstrap_admins=(PRINCIPAL,), users=None, raw_text=None):
    path = _home() / "dashboard-governance.yaml"
    if raw_text is not None:
        path.write_text(raw_text, encoding="utf-8")
        return path
    data = {
        "version": 1,
        "mode": mode,
        "default_effect": "deny",
        "bootstrap_admins": list(bootstrap_admins),
        "roles": {"tech_lead": {"grants": {"tools": ["terminal"]}}},
        "users": users if users is not None else {"alice@example.test": {"roles": ["tech_lead"]}},
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _write_config(principal=PRINCIPAL):
    cfg = {"model": "test-model"}
    if principal is not None:
        cfg["cron"] = {"system_principal": principal}
    (_home() / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _patch_run(monkeypatch):
    """Record every agent run and the governance it ran under."""
    calls = []

    def fake_run_job(job, **_kw):
        from hermes_cli.dashboard_governance.context import current_governance_context

        ctx = current_governance_context()
        calls.append((job["id"], ctx.access.subject.email if ctx else None))
        return (True, "output", "final response", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "_deliver_result", lambda *_a, **_kw: None)
    return calls


def _governed(email, *, admin=False, mode="enforce"):
    from hermes_cli.dashboard_governance.context import DashboardGovernanceContext
    from hermes_cli.dashboard_governance.models import (
        EffectiveAccess,
        GovernanceSubject,
        GrantSet,
    )

    access = EffectiveAccess(
        subject=GovernanceSubject(email=email),
        mode=mode,
        roles=frozenset({"owner", "admin"} if admin else {"tech_lead"}),
        grants=GrantSet(),
        grant_sources=("bootstrap_admin",) if admin else (),
    )
    return DashboardGovernanceContext(subject=access.subject, access=access)


def _job(**kw):
    from cron.jobs import create_job

    kw.setdefault("prompt", "Summarise the inbox")
    kw.setdefault("schedule", "every 1h")
    return create_job(**kw)


def _get(job_id):
    from cron.jobs import get_job

    return get_job(job_id)


# ---------------------------------------------------------------------------
# The fire gate
# ---------------------------------------------------------------------------


def test_an_ownerless_agent_job_is_refused_under_enforce(monkeypatch):
    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job()
    assert job["owner_email"] == ""

    with pytest.raises(s.CronJobOwnerRefused):
        s.run_one_job(_get(job["id"]))

    assert calls == [], "the agent must not run"
    stored = _get(job["id"])
    assert stored["last_status"] == "blocked_config"
    assert "no owner" in stored["last_error"]
    assert stored["failure_streak"] == 1
    assert stored["next_run_at"], "a recurring job stays scheduled"

    from cron.executions import list_executions
    from cron.incidents import list_incidents

    executions = list_executions(job_id=job["id"])
    assert executions and executions[0]["status"] == "failed"
    assert "no owner" in executions[0]["error"]
    assert [i["job_id"] for i in list_incidents()] == [job["id"]]


def test_the_refusal_text_is_plain_and_names_no_internals(monkeypatch):
    _write_policy("enforce")
    _patch_run(monkeypatch)
    job = _job()
    with pytest.raises(s.CronJobOwnerRefused) as exc:
        s.run_one_job(_get(job["id"]))
    text = str(exc.value)
    assert text == _get(job["id"])["last_error"]
    for word in ("hermes", "Hermes", "governance.yaml", chr(0x2013), chr(0x2014)):
        assert word not in text


def test_a_due_ownerless_agent_job_is_refused_by_the_ticker(monkeypatch):
    from cron.jobs import _hermes_now, update_job

    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job()
    update_job(job["id"], {"next_run_at": (_hermes_now() - timedelta(minutes=1)).isoformat()})

    s.tick(verbose=False, sync=True)

    assert calls == []
    stored = _get(job["id"])
    assert stored["last_status"] == "blocked_config"
    assert stored.get("fire_claim") is None


def test_a_manual_run_of_an_ownerless_job_is_refused_and_recorded_once(monkeypatch):
    from tools.cronjob_tools import cronjob

    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job()

    result = json.loads(cronjob(action="run", job_id=job["id"]))

    assert calls == []
    assert result["job"]["execution_success"] is False
    assert "no owner" in result["job"]["execution_error"]
    stored = _get(job["id"])
    assert stored["failure_streak"] == 1
    assert stored["last_status"] == "blocked_config"


@pytest.mark.parametrize("mode", ["off", "report_only"])
def test_ownerless_jobs_run_as_before_without_enforce(monkeypatch, mode):
    _write_policy(mode)
    calls = _patch_run(monkeypatch)
    job = _job()

    assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], None)]
    assert _get(job["id"])["last_status"] == "ok"


def test_ownerless_jobs_run_as_before_without_a_policy_file(monkeypatch):
    calls = _patch_run(monkeypatch)
    job = _job()

    assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], None)]


def test_an_ownerless_script_job_still_runs_under_enforce(monkeypatch):
    """A no_agent script has no agent to govern; it is not refused."""
    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job(prompt="", script="watchdog.sh", no_agent=True)

    assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], None)]


def test_a_job_owned_by_the_system_principal_runs_under_its_governance(monkeypatch):
    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job(owner_email=PRINCIPAL)

    assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], PRINCIPAL)]


def test_a_job_owned_by_a_person_runs_under_their_governance(monkeypatch):
    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job(owner_email="alice@example.test")

    assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], "alice@example.test")]


def test_an_unreadable_policy_refuses_an_ownerless_agent_job(monkeypatch):
    """Fail closed: without a readable policy nobody can tell whether enforce
    applies, so the ownerless job does not run unbound."""
    _write_policy(raw_text="mode: [enforce\n")
    calls = _patch_run(monkeypatch)
    job = _job()

    with pytest.raises(s.CronJobOwnerRefused):
        s.run_one_job(_get(job["id"]))
    assert calls == []
    stored = _get(job["id"])
    assert stored["last_status"] == "blocked_config"
    assert "policy" in stored["last_error"]


def test_an_unreadable_policy_refuses_an_owned_job_and_records_it(monkeypatch):
    """This already stopped the run; it is now also visible on the job."""
    _write_policy(raw_text="mode: [enforce\n")
    calls = _patch_run(monkeypatch)
    job = _job(owner_email="alice@example.test")

    with pytest.raises(s.CronJobOwnerRefused):
        s.run_one_job(_get(job["id"]))
    assert calls == []
    assert "policy" in _get(job["id"])["last_error"]


def test_the_gate_unbinds_after_the_run(monkeypatch):
    from hermes_cli.dashboard_governance.context import current_governance_context

    _write_policy("enforce")
    with s._governed_as_job_owner({"id": "j1", "owner_email": PRINCIPAL}):
        assert current_governance_context().access.subject.email == PRINCIPAL
    assert current_governance_context() is None


# ---------------------------------------------------------------------------
# Owner stamping at create time
# ---------------------------------------------------------------------------


def test_a_governed_admin_job_gets_the_system_principal():
    """Admins used to make ownerless jobs; under enforce those would now be
    refused, so the job is given to the system principal instead."""
    from hermes_cli.dashboard_governance.context import governance_context

    _write_config(PRINCIPAL)
    with governance_context(_governed("root@example.test", admin=True)):
        job = _job()
    assert job["owner_email"] == PRINCIPAL


def test_a_governed_admin_job_stays_ownerless_without_a_principal():
    from hermes_cli.dashboard_governance.context import governance_context

    _write_config(None)
    with governance_context(_governed("root@example.test", admin=True)):
        job = _job()
    assert job["owner_email"] == ""


def test_a_governed_person_still_owns_their_own_job():
    from hermes_cli.dashboard_governance.context import governance_context

    _write_config(PRINCIPAL)
    with governance_context(_governed("alice@example.test")):
        job = _job()
    assert job["owner_email"] == "alice@example.test"


def test_an_admin_outside_enforce_keeps_the_old_behaviour():
    from hermes_cli.dashboard_governance.context import governance_context

    _write_config(PRINCIPAL)
    with governance_context(_governed("root@example.test", admin=True, mode="report_only")):
        job = _job()
    assert job["owner_email"] == ""


def test_an_ungoverned_create_is_not_stamped_outside_the_cli():
    """A gateway sender with no mapped identity is not the operator: fail
    closed and leave the job ownerless (refused under enforce)."""
    _write_config(PRINCIPAL)
    assert _job()["owner_email"] == ""


def test_the_cli_scope_stamps_the_system_principal():
    from cron.jobs import system_principal_create_scope

    _write_config(PRINCIPAL)
    with system_principal_create_scope():
        job = _job()
    assert job["owner_email"] == PRINCIPAL
    assert _job()["owner_email"] == "", "the scope ends with the block"


def test_the_cli_scope_never_overrides_a_governed_person():
    from cron.jobs import system_principal_create_scope
    from hermes_cli.dashboard_governance.context import governance_context

    _write_config(PRINCIPAL)
    with system_principal_create_scope(), governance_context(_governed("alice@example.test")):
        job = _job()
    assert job["owner_email"] == "alice@example.test"


def test_an_explicit_owner_always_wins():
    from cron.jobs import system_principal_create_scope

    _write_config(PRINCIPAL)
    with system_principal_create_scope():
        job = _job(owner_email="Bob@Example.Test")
    assert job["owner_email"] == "bob@example.test"


def test_an_invalid_configured_principal_is_ignored():
    from cron.jobs import cron_system_principal, system_principal_create_scope

    _write_config("not an address")
    assert cron_system_principal() == ""
    with system_principal_create_scope():
        assert _job()["owner_email"] == ""


def _cli(argv):
    import argparse

    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cron_command)
    args = parser.parse_args(argv)
    return args.func(args)


@pytest.mark.parametrize("verb", ["create", "add"])
def test_hermes_cron_create_stamps_the_system_principal(capsys, verb):
    from cron.jobs import list_jobs

    _write_config(PRINCIPAL)
    assert _cli(["cron", verb, "every 1h", "Summarise the inbox", "--name", "cli job"]) == 0
    assert "Created job" in capsys.readouterr().out
    (job,) = [j for j in list_jobs(include_disabled=True) if j["name"] == "cli job"]
    assert job["owner_email"] == PRINCIPAL


def test_hermes_cron_create_without_a_principal_stays_ownerless():
    from cron.jobs import list_jobs

    _write_config(None)
    _cli(["cron", "create", "every 1h", "Summarise the inbox", "--name", "cli job"])
    (job,) = [j for j in list_jobs(include_disabled=True) if j["name"] == "cli job"]
    assert job["owner_email"] == ""


def test_an_admin_behind_a_bot_ceiling_does_not_get_the_principal():
    """A principal-owned job runs as an administrator, without the bot's
    ceiling. The session was narrower than that, so the job stays ownerless
    (and enforce refuses it) instead of widening to the principal."""
    from dataclasses import replace

    from hermes_cli.dashboard_governance.context import governance_context
    from hermes_cli.dashboard_governance.models import (
        EffectiveAccess,
        GovernanceSubject,
        GrantSet,
    )

    _write_config(PRINCIPAL)
    ceiling = EffectiveAccess(
        subject=GovernanceSubject(email="bot@example.test"),
        mode="enforce",
        roles=frozenset({"bot"}),
        grants=GrantSet(tools=frozenset({"web_search"})),
    )
    ctx = replace(
        _governed("root@example.test", admin=True),
        bot_access_ceiling=ceiling,
        bot_access_check=lambda: True,
    )
    with governance_context(ctx):
        job = _job()
    assert job["owner_email"] == ""


def test_an_admin_turn_continuing_a_non_admin_envelope_does_not_get_the_principal():
    from dataclasses import replace

    from hermes_cli.dashboard_governance.context import (
        governance_context,
        serialize_context_for_env,
    )

    _write_config(PRINCIPAL)
    narrower = serialize_context_for_env(_governed("root@example.test"))
    ctx = replace(
        _governed("root@example.test", admin=True),
        continuation_contexts=(narrower,),
    )
    with governance_context(ctx):
        job = _job()
    assert job["owner_email"] == ""


# ---------------------------------------------------------------------------
# A governed person's shell never gets the system principal
#
# tools/environments/local.py gives every child process of a governed
# non-admin session under enforce HERMES_DWD_IDENTITY, but no governance
# context. `hermes cron create` in that shell is that person, not the operator
# at the host shell, so the job must be theirs (or refused), never the
# administrator principal's.
# ---------------------------------------------------------------------------

MALLORY = "mallory@example.test"


def _governed_shell(monkeypatch, identity=MALLORY):
    _write_config(PRINCIPAL)
    _write_policy("enforce", users={MALLORY: {"roles": ["tech_lead"]}})
    monkeypatch.setenv("HERMES_DWD_IDENTITY", identity)


@pytest.mark.parametrize("verb", ["create", "add"])
def test_hermes_cron_create_in_a_governed_shell_belongs_to_that_person(monkeypatch, capsys, verb):
    from cron.jobs import list_jobs
    from hermes_cli.dashboard_governance.context import current_governance_context

    _governed_shell(monkeypatch, "Mallory@Example.Test")
    assert current_governance_context() is None, "the shell has no in-process context"

    assert _cli(["cron", verb, "every 1h", "Read every mailbox", "--name", "shell job"]) == 0
    assert "Created job" in capsys.readouterr().out
    (job,) = [j for j in list_jobs(include_disabled=True) if j["name"] == "shell job"]
    assert job["owner_email"] == MALLORY
    assert job["owner_email"] != PRINCIPAL


@pytest.mark.parametrize("identity", ["unresolved-identity", "not an address", "a@b@c"])
def test_hermes_cron_create_in_a_shell_with_an_unusable_identity_is_refused(monkeypatch, capsys, identity):
    from cron.jobs import list_jobs

    _governed_shell(monkeypatch, identity)

    assert _cli(["cron", "create", "every 1h", "Read every mailbox", "--name", "shell job"]) == 1
    assert "no verified account" in capsys.readouterr().out
    assert list_jobs(include_disabled=True) == []


@pytest.mark.parametrize("identity", ["", "   "])
def test_an_empty_shell_identity_is_the_operator(monkeypatch, identity):
    from cron.jobs import system_principal_create_scope

    _governed_shell(monkeypatch, identity)
    with system_principal_create_scope():
        assert _job()["owner_email"] == PRINCIPAL


def test_the_cli_scope_never_stamps_the_principal_in_a_governed_shell(monkeypatch):
    from cron.jobs import system_principal_create_scope

    _governed_shell(monkeypatch)
    with system_principal_create_scope():
        job = _job()
    assert job["owner_email"] == MALLORY


def test_the_cli_scope_refuses_a_shell_whose_identity_is_unresolved(monkeypatch):
    from cron.jobs import list_jobs, system_principal_create_scope

    _governed_shell(monkeypatch, "unresolved-identity")
    with system_principal_create_scope(), pytest.raises(ValueError, match="no verified account"):
        _job()
    assert list_jobs(include_disabled=True) == []


def test_a_governed_shell_under_an_admin_context_still_owns_its_job(monkeypatch):
    """The shell identity marks a governed non-admin. An administrator context
    bound in the same process does not turn the job into the principal's."""
    from hermes_cli.dashboard_governance.context import governance_context

    _governed_shell(monkeypatch)
    with governance_context(_governed("root@example.test", admin=True)):
        job = _job()
    assert job["owner_email"] == MALLORY


@pytest.mark.parametrize("claimed", [PRINCIPAL, "alice@example.test"])
def test_a_governed_shell_cannot_give_its_job_to_someone_else(monkeypatch, claimed):
    from cron.jobs import list_jobs, system_principal_create_scope

    _governed_shell(monkeypatch)
    with system_principal_create_scope(), pytest.raises(ValueError, match="own account"):
        _job(owner_email=claimed)
    assert list_jobs(include_disabled=True) == []


def test_a_governed_shell_and_a_different_governed_context_conflict(monkeypatch):
    from hermes_cli.dashboard_governance.context import governance_context

    _governed_shell(monkeypatch)
    with governance_context(_governed("alice@example.test")), pytest.raises(ValueError):
        _job()


def test_a_governed_shell_may_name_itself_as_owner(monkeypatch):
    _governed_shell(monkeypatch)
    assert _job(owner_email="MALLORY@example.test")["owner_email"] == MALLORY


def test_a_job_made_in_a_governed_shell_fires_as_that_person(monkeypatch):
    """End to end: never as the administrator principal."""
    from cron.jobs import list_jobs

    _governed_shell(monkeypatch)
    _cli(["cron", "create", "every 1h", "Read every mailbox", "--name", "shell job"])
    monkeypatch.delenv("HERMES_DWD_IDENTITY")  # the ticker is not that shell
    calls = _patch_run(monkeypatch)
    (job,) = [j for j in list_jobs(include_disabled=True) if j["name"] == "shell job"]

    assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], MALLORY)]


# ---------------------------------------------------------------------------
# scripts/cron_assign_system_owner.py: dry run by default, --apply at go-live
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cron_assign_system_owner.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("cron_assign_system_owner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_job(job_id, *, owner="", no_agent=False, name=None):
    return {
        "id": job_id,
        "name": name or f"job {job_id}",
        "prompt": "" if no_agent else "Summarise the inbox",
        "script": "watchdog.sh" if no_agent else None,
        "no_agent": no_agent,
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
        "schedule_display": "every 60m",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "created_at": "2026-09-01T09:00:00+00:00",
        "next_run_at": "2026-09-27T10:00:00+00:00",
        "deliver": "local",
        "origin": None,
        "owner_email": owner,
    }


def _make_store(home, jobs, *, principal=PRINCIPAL, mode="enforce", admins=(PRINCIPAL,)):
    (home / "cron").mkdir(parents=True, exist_ok=True)
    cfg = {"model": "test-model"}
    if principal:
        cfg["cron"] = {"system_principal": principal}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    policy = {"version": 1, "mode": mode, "default_effect": "deny", "bootstrap_admins": list(admins)}
    (home / "dashboard-governance.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}, indent=2), encoding="utf-8")
    return home


def _tree(root):
    """Every path under root with type, mode, size, mtime and content hash."""
    snapshot = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            st = path.lstat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(st.st_mode) else ""
            snapshot[str(path.relative_to(root))] = (
                stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns, digest
            )
    return snapshot


def _owners(home):
    data = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))
    return {job["id"]: job.get("owner_email") for job in data["jobs"]}


def test_the_migration_dry_run_changes_nothing(tmp_path):
    """Run as the operator would, in a fresh process: the report is complete
    and not one byte, mode or timestamp changes, in the store or in HOME."""
    store = _make_store(
        tmp_path / "store",
        [_raw_job("agent1"), _raw_job("script1", no_agent=True), _raw_job("owned1", owner="alice@example.test")],
    )
    _make_store(store / "profiles" / "worker", [_raw_job("agent2")])
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    before, home_before = _tree(store), _tree(fake_home)

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(fake_home),
        "HERMES_HOME": str(store),
        "PYTHONPATH": str(REPO_ROOT),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
    }
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--all-profiles"],
        env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=120,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Dry run: nothing was changed" in proc.stdout
    for job_id in ("agent1", "script1", "agent2"):
        assert job_id in proc.stdout
    assert "owned1" not in proc.stdout
    assert _tree(store) == before
    assert _tree(fake_home) == home_before


def test_the_migration_dry_run_reports_what_apply_would_do(capsys):
    from cron.jobs import create_job

    script = _load_script()
    _write_config(PRINCIPAL)
    _write_policy("enforce")
    agent = create_job(prompt="legacy agent", schedule="every 1h")
    job_script = create_job(prompt="", script="watchdog.sh", no_agent=True, schedule="every 1h")
    create_job(prompt="owned", schedule="every 1h", owner_email="alice@example.test")
    owners_before = _owners(_home())

    assert script.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry_run" and report["ok"] is True
    (store,) = report["stores"]
    assert store["principal"] == PRINCIPAL
    assert store["governance_mode"] == "enforce"
    assert store["principal_policy_entry"] == "admin"
    assert {(j["id"], j["kind"], j["action"]) for j in store["jobs"]} == {
        (agent["id"], "agent", "would_assign"),
        (job_script["id"], "script", "would_assign"),
    }
    assert _owners(_home()) == owners_before

    assert script.main(["--json", "--agent-only"]) == 0
    (store,) = json.loads(capsys.readouterr().out)["stores"]
    assert [j["id"] for j in store["jobs"]] == [agent["id"]]


def test_the_migration_apply_assigns_the_principal_and_audits(capsys):
    from cron.jobs import create_job, owner_audit_file

    script = _load_script()
    _write_config(PRINCIPAL)
    _write_policy("enforce")
    agent = create_job(prompt="legacy agent", schedule="every 1h")
    job_script = create_job(prompt="", script="watchdog.sh", no_agent=True, schedule="every 1h")
    owned = create_job(prompt="owned", schedule="every 1h", owner_email="alice@example.test")

    assert script.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "Assigned the system principal to 2 job(s)" in out
    assert "Ownerless agent jobs left: 0" in out
    owners = _owners(_home())
    assert owners[agent["id"]] == PRINCIPAL
    assert owners[job_script["id"]] == PRINCIPAL
    assert owners[owned["id"]] == "alice@example.test"
    rows = [json.loads(line) for line in owner_audit_file().read_text().splitlines()]
    assert {r["job_id"] for r in rows} == {agent["id"], job_script["id"]}
    assert all(r["source"] == "cron_assign_system_owner" for r in rows)
    assert all(r["previous_owner"] == "" and r["new_owner"] == PRINCIPAL for r in rows)
    assert all(r["actor"].startswith("os:") for r in rows)

    # A second run finds nothing to do and writes no new audit rows.
    assert script.main(["--apply"]) == 0
    assert "Ownerless jobs: 0" in capsys.readouterr().out
    assert len(owner_audit_file().read_text().splitlines()) == 2


def test_the_migration_covers_every_profile(capsys):
    from cron.jobs import create_job

    script = _load_script()
    _write_config(PRINCIPAL)
    _write_policy("enforce")
    root_job = create_job(prompt="root", schedule="every 1h")
    profile = _make_store(_home() / "profiles" / "worker", [_raw_job("agent2")])

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert _owners(_home())[root_job["id"]] == PRINCIPAL
    assert _owners(profile)["agent2"] == PRINCIPAL
    assert (profile / "cron" / "owner-audit.jsonl").is_file()


def test_the_migration_takes_an_explicit_principal(capsys):
    from cron.jobs import create_job

    script = _load_script()
    _write_config(None)
    _write_policy("enforce", bootstrap_admins=("ops@example.test",))
    job = create_job(prompt="legacy", schedule="every 1h")

    assert script.main(["--apply", "--principal", "Ops@Example.Test"]) == 0
    assert _owners(_home())[job["id"]] == "ops@example.test"
    assert "cron.system_principal is not set" in capsys.readouterr().out


def test_the_migration_rejects_an_invalid_principal_argument(capsys):
    script = _load_script()
    assert script.main(["--principal", "not an address"]) == 2


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda: (_write_config(None), _write_policy("enforce")), "No system principal"),
        (lambda: (_write_config(PRINCIPAL), _write_policy("enforce", bootstrap_admins=())), "no entry"),
        (lambda: (_write_config(PRINCIPAL), _write_policy(raw_text="mode: [enforce\n")), "policy"),
    ],
)
def test_the_migration_refuses_to_apply_when_something_blocks(capsys, setup, message):
    from cron.jobs import create_job, owner_audit_file

    script = _load_script()
    setup()
    job = create_job(prompt="legacy", schedule="every 1h")

    assert script.main([]) == 1, "the dry run already says apply would be refused"
    assert script.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert message in out and "Nothing was changed" in out
    assert _owners(_home())[job["id"]] == ""
    assert not owner_audit_file().exists()


def test_a_missing_policy_entry_only_warns_outside_enforce(capsys):
    from cron.jobs import create_job

    script = _load_script()
    _write_config(PRINCIPAL)
    _write_policy("off", bootstrap_admins=())
    job = create_job(prompt="legacy", schedule="every 1h")

    assert script.main(["--apply"]) == 0
    assert "no entry in the governance policy" in capsys.readouterr().out
    assert _owners(_home())[job["id"]] == PRINCIPAL


def test_the_migration_warns_about_a_restricted_principal(capsys):
    from cron.jobs import create_job

    script = _load_script()
    _write_config(PRINCIPAL)
    _write_policy("enforce", bootstrap_admins=(), users={PRINCIPAL: {"roles": ["tech_lead"]}})
    create_job(prompt="legacy", schedule="every 1h")

    assert script.main([]) == 0
    out = capsys.readouterr().out
    assert "principal policy entry: restricted" in out
    assert "not an administrator" in out


def test_the_migration_refuses_to_apply_from_a_governed_shell(monkeypatch):
    from cron.jobs import create_job

    script = _load_script()
    _write_config(PRINCIPAL)
    _write_policy("enforce")
    job = create_job(prompt="legacy", schedule="every 1h")
    monkeypatch.setenv("HERMES_DWD_IDENTITY", "mallory@example.test")

    assert script.main(["--apply"]) == 1
    assert _owners(_home())[job["id"]] == ""


def test_the_migration_never_overwrites_an_owner_set_meanwhile():
    from cron.jobs import create_job, reassign_job_owner

    script = _load_script()
    _write_config(PRINCIPAL)
    _write_policy("enforce")
    job = create_job(prompt="legacy", schedule="every 1h")
    report = script.inspect_store(_home(), principal_override="", agent_only=False)
    reassign_job_owner(job["id"], "alice@example.test", actor="os:tester")

    script.apply_store(report, actor="os:tester", reason="test")

    (item,) = report["jobs"]
    assert item["action"] == "skipped"
    assert _owners(_home())[job["id"]] == "alice@example.test"
