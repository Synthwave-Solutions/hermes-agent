"""Under governance ``enforce`` an agent job must have an owner to run.

``_governed_as_job_owner`` binds the owner's governance for every fire. A job
without an owner used to run with no governance at all, i.e. with the full
rights of the platform owner. Under ``enforce`` such an agent job is now
refused and the refusal is recorded on the job, its execution and the
incident store. Jobs owned by the configured system principal
(``cron.system_principal``) run under that principal's governance like any
other owner. Jobs made at the CLI, or by a governed administrator, are stamped
with the system principal so they do not end up ownerless.
"""

from __future__ import annotations

import json
from datetime import timedelta

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
    from hermes_cli.dashboard_governance.context import governance_context

    from cron.jobs import system_principal_create_scope

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
