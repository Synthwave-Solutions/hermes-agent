"""Under governance ``enforce`` an agent job must have an owner to run.

``_governed_as_job_owner`` binds the owner's governance for every fire. A job
without an owner used to run with no governance at all, i.e. with the full
rights of the platform owner. Under ``enforce`` such an agent job is now
refused and the refusal is recorded on the job, its execution and the
incident store. Jobs owned by the configured system principal
(``cron.system_principal``) run under that principal's governance like any
other owner. Jobs made by the operator at the host shell, or by a governed
administrator, are stamped with the system principal so they do not end up
ownerless; a job made in a governed person's shell is theirs, and one made
below any other agent session stays ownerless, never the principal's. A named
profile store without a policy of its own follows the platform root's policy.
A refusal is never a run: it uses up no repeat and completes no one-shot.
``run_job_governed`` is the one governed entry point for running a job on
demand, and it returns a refusal instead of raising it.
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

    # Processed, like any failed run: the refusal is recorded on the job, so
    # the caller (ticker, provider, cronjob tool) has nothing left to handle.
    assert s.run_one_job(_get(job["id"])) is True

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
    result = s.run_job_governed(_get(job["id"]), reason="manual")
    text = result.refusal
    assert text and text == _get(job["id"])["last_error"]
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

    assert s.run_one_job(_get(job["id"])) is True
    assert calls == []
    stored = _get(job["id"])
    assert stored["last_status"] == "blocked_config"
    assert "policy" in stored["last_error"]


def test_an_unreadable_policy_refuses_an_owned_job_and_records_it(monkeypatch):
    """This already stopped the run; it is now also visible on the job."""
    _write_policy(raw_text="mode: [enforce\n")
    calls = _patch_run(monkeypatch)
    job = _job(owner_email="alice@example.test")

    assert s.run_one_job(_get(job["id"])) is True
    assert calls == []
    assert "policy" in _get(job["id"])["last_error"]


# ---------------------------------------------------------------------------
# run_job_governed: the one entry point for running a job on demand
#
# The WebUI "Run now" path runs a job itself instead of through the ticker.
# It must get the same gate as a scheduled fire, and a refusal must come back
# as a result, never as an exception or an ERROR traceback.
# ---------------------------------------------------------------------------


def test_run_job_governed_runs_the_job_under_its_owner(monkeypatch):
    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job(owner_email="alice@example.test")

    result = s.run_job_governed(_get(job["id"]), reason="manual")

    assert calls == [(job["id"], "alice@example.test")]
    assert result.outcome == "ran" and result.ran and not result.refused
    assert result.success is True
    assert result.as_run_job_tuple() == (True, "output", "final response", None)
    assert result.reason == "manual"
    assert result.owner_email == "alice@example.test"
    assert result.refusal is None and result.refusal_recorded is False
    from hermes_cli.dashboard_governance.context import current_governance_context

    assert current_governance_context() is None, "the owner is unbound afterwards"


def test_run_job_governed_refuses_an_ownerless_agent_job_without_raising(monkeypatch, caplog):
    import logging

    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job()

    with caplog.at_level(logging.DEBUG, logger="cron.scheduler"):
        result = s.run_job_governed(_get(job["id"]), reason="manual")

    assert calls == [], "the agent must not run"
    assert result.outcome == "refused" and result.refused and not result.ran
    assert result.success is False
    assert "no owner" in result.refusal and result.error == result.refusal
    assert result.output == "" and result.final_response == ""
    assert result.refusal_recorded is True
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [r for r in caplog.records if r.exc_info], "no traceback for a refusal"
    stored = _get(job["id"])
    assert stored["last_status"] == "blocked_config"
    assert stored["last_error"] == result.refusal
    assert stored["failure_streak"] == 1

    from cron.executions import list_executions
    from cron.incidents import list_incidents

    assert [e["status"] for e in list_executions(job_id=job["id"])] == ["failed"]
    assert [i["job_id"] for i in list_incidents()] == [job["id"]]


@pytest.mark.parametrize("schedule", ["every 1h", "in 30m"])
def test_an_on_demand_refusal_leaves_the_schedule_alone(monkeypatch, schedule):
    """Only the outcome is recorded: a run someone asked for now does not move,
    pause or complete the job's own schedule, or use up a repeat."""
    _write_policy("enforce")
    _patch_run(monkeypatch)
    job = _job(schedule=schedule, repeat=3)
    before = _get(job["id"])

    assert s.run_job_governed(before, reason="manual").refused
    stored = _get(job["id"])
    for field in ("enabled", "state", "next_run_at", "repeat", "last_run_at", "fire_claim", "run_claim"):
        assert stored.get(field) == before.get(field), field
    assert stored["last_status"] == "blocked_config"


def test_run_job_governed_can_leave_the_record_to_the_caller(monkeypatch):
    """A caller whose active store does not hold the job records the refusal
    in the store that does, with ``cron.jobs.mark_job_refused``."""
    from cron.executions import list_executions
    from cron.jobs import mark_job_refused

    _write_policy("enforce")
    _patch_run(monkeypatch)
    job = _job()
    before = _get(job["id"])

    result = s.run_job_governed(before, reason="manual", record_refusal=False)

    assert result.refused and result.refusal_recorded is False
    assert _get(job["id"]) == before, "nothing was written"
    assert list_executions(job_id=job["id"]) == []

    assert mark_job_refused(job["id"], result.refusal, consume_occurrence=False) is True
    stored = _get(job["id"])
    assert stored["last_status"] == "blocked_config"
    assert stored["next_run_at"] == before["next_run_at"]


@pytest.mark.parametrize("mode", ["off", "report_only"])
def test_run_job_governed_runs_ownerless_jobs_as_before_without_enforce(monkeypatch, mode):
    _write_policy(mode)
    calls = _patch_run(monkeypatch)
    job = _job()

    result = s.run_job_governed(_get(job["id"]), reason="api")
    assert result.ran and result.success
    assert calls == [(job["id"], None)]


def test_run_job_governed_refuses_an_owner_whose_governance_cannot_be_resolved(monkeypatch):
    _write_policy(raw_text="mode: [enforce\n")
    calls = _patch_run(monkeypatch)
    job = _job(owner_email="alice@example.test")

    result = s.run_job_governed(_get(job["id"]), reason="manual")
    assert calls == []
    assert result.refused and "policy" in result.refusal


def test_run_job_governed_returns_a_failed_run_instead_of_raising(monkeypatch):
    _write_policy("enforce")

    def exploding_run_job(job, **_kw):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(s, "run_job", exploding_run_job)
    job = _job(owner_email="alice@example.test")

    result = s.run_job_governed(_get(job["id"]), reason="manual")
    assert result.outcome == "failed" and not result.ran and not result.refused
    assert result.success is False
    assert "provider exploded" in result.error
    from hermes_cli.dashboard_governance.context import current_governance_context

    assert current_governance_context() is None


def test_run_job_governed_passes_run_job_arguments_through(monkeypatch):
    seen = {}

    def fake_run_job(job, **kw):
        seen.update(kw)
        return (True, "doc", "final", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    job = _job(owner_email="alice@example.test")

    s.run_job_governed(_get(job["id"]), reason="manual", extra_prompt="only this time")
    assert seen == {"extra_prompt": "only this time"}


def test_the_run_result_travels_as_plain_data(monkeypatch):
    """The WebUI runs the job in a child process and sends the result back."""
    import pickle

    _write_policy("enforce")
    _patch_run(monkeypatch)
    refused = s.run_job_governed(_get(_job()["id"]), reason="manual")

    data = refused.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["outcome"] == "refused" and data["refusal"] == refused.refusal
    assert pickle.loads(pickle.dumps(refused)) == refused


def test_there_is_one_public_governed_entry_point():
    assert not hasattr(s, "governed_as_job_owner")
    assert callable(s.run_job_governed)


def test_a_refused_fire_is_not_logged_as_a_failed_future(monkeypatch, caplog):
    """The ticker counts a refused fire as processed: the refusal is on the
    job, so no ERROR "Cron job future failed" traceback per fire."""
    import logging

    from cron.jobs import _hermes_now, update_job

    _write_policy("enforce")
    _patch_run(monkeypatch)
    job = _job()
    update_job(job["id"], {"next_run_at": (_hermes_now() - timedelta(minutes=1)).isoformat()})

    with caplog.at_level(logging.DEBUG, logger="cron.scheduler"):
        assert s.tick(verbose=False, sync=True) == 1

    assert not [r for r in caplog.records if "future failed" in r.getMessage()]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    refusals = [r for r in caplog.records if "no owner" in r.getMessage()]
    assert refusals and all(r.levelno == logging.WARNING and not r.exc_info for r in refusals)
    assert _get(job["id"])["last_status"] == "blocked_config"


def test_a_provider_fire_of_a_refused_job_returns_processed(monkeypatch):
    """``fire_claimed`` (dashboard and API server fires) no longer raises."""
    from cron.scheduler_provider import InProcessCronScheduler

    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job()
    provider = InProcessCronScheduler()
    claimed = provider.claim_fire(job["id"], force=True)
    assert isinstance(claimed, dict)

    assert provider.fire_claimed(claimed) is True
    assert calls == []
    stored = _get(job["id"])
    assert stored["last_status"] == "blocked_config"
    assert stored.get("fire_claim") is None


def test_a_refused_fire_does_not_use_up_a_repeat_limit(monkeypatch):
    """A refusal is not a run: a job limited to N runs keeps all N for when an
    administrator has given it an owner."""
    from cron.jobs import reassign_job_owner

    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job(repeat=2)

    for _ in range(3):
        assert s.run_one_job(_get(job["id"])) is True
    stored = _get(job["id"])
    assert calls == []
    assert stored["repeat"]["completed"] == 0
    assert stored["enabled"] is True
    assert stored["state"] == "scheduled"
    assert stored["next_run_at"]

    reassign_job_owner(job["id"], PRINCIPAL, actor="os:test")
    assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], PRINCIPAL)]
    assert _get(job["id"])["repeat"]["completed"] == 1


def test_a_refused_recurring_fire_through_the_ticker_keeps_its_repeats(monkeypatch):
    from cron.jobs import _hermes_now, update_job

    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job(repeat=2)
    for _ in range(3):
        update_job(job["id"], {"next_run_at": (_hermes_now() - timedelta(minutes=1)).isoformat()})
        assert s.tick(verbose=False, sync=True) == 1

    stored = _get(job["id"])
    assert calls == []
    assert stored["repeat"]["completed"] == 0
    assert stored["enabled"] is True and stored["state"] == "scheduled"
    assert stored["failure_streak"] == 3


def test_a_refused_one_shot_is_not_counted_as_a_run(monkeypatch):
    """A refused one-shot keeps its run: not counted, not completed, and not
    left due (it would be refused every tick until it aged out and vanished).
    It is paused with the reason instead."""
    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    job = _job(schedule="in 30m")
    assert job["schedule"]["kind"] == "once"

    assert s.run_one_job(_get(job["id"])) is True
    stored = _get(job["id"])
    assert calls == []
    assert stored["repeat"]["completed"] == 0
    assert stored["last_status"] == "blocked_config"
    assert stored["state"] == "paused" and stored["enabled"] is False
    assert stored["paused_reason"] == stored["last_error"]
    assert stored.get("last_run_at") is None, "a one-shot that never ran must stay runnable"


def _make_one_shot_due(job_id, *, seconds_ago=30):
    """Put a one-shot's run time just inside its grace window, as the ticker
    sees it on the minute it is due."""
    from cron.jobs import _hermes_now, load_jobs, save_jobs

    due_at = (_hermes_now() - timedelta(seconds=seconds_ago)).isoformat()
    jobs = load_jobs()
    for record in jobs:
        if record["id"] == job_id:
            record["schedule"]["run_at"] = due_at
            record["next_run_at"] = due_at
    save_jobs(jobs)


def test_a_refused_one_shot_through_the_ticker_keeps_its_run(monkeypatch):
    """Through the real ticker path (due scan, run claim, fire claim,
    run_one_job): the gate refuses before the pre-run dispatch claim, so
    ``repeat.completed`` is never pre-incremented, the one-shot is neither
    completed nor removed as a wedged dispatch, and once an administrator has
    given it an owner it runs exactly once."""
    from cron.jobs import get_due_jobs, reassign_job_owner, trigger_job

    _write_policy("enforce")
    calls = _patch_run(monkeypatch)
    dispatch_claims = []
    real_claim_dispatch = s.claim_dispatch

    def recording_claim_dispatch(job_id):
        dispatch_claims.append(job_id)
        return real_claim_dispatch(job_id)

    monkeypatch.setattr(s, "claim_dispatch", recording_claim_dispatch)
    job = _job(schedule="in 30m")
    assert job["repeat"] == {"times": 1, "completed": 0}
    _make_one_shot_due(job["id"])

    assert s.tick(verbose=False, sync=True) == 1

    assert calls == [] and dispatch_claims == []
    stored = _get(job["id"])
    assert stored is not None, "not removed as a wedged dispatch"
    assert stored["repeat"]["completed"] == 0
    assert stored["state"] == "paused" and stored["enabled"] is False
    assert stored.get("last_run_at") is None
    assert stored["last_status"] == "blocked_config"
    assert stored.get("fire_claim") is None and stored.get("run_claim") is None
    assert get_due_jobs() == []

    # The next ticks leave it alone instead of refusing it every minute.
    assert s.tick(verbose=False, sync=True) == 0
    assert _get(job["id"])["failure_streak"] == 1

    reassign_job_owner(job["id"], PRINCIPAL, actor="os:test")
    trigger_job(job["id"])
    assert s.tick(verbose=False, sync=True) == 1

    assert calls == [(job["id"], PRINCIPAL)]
    assert dispatch_claims == [job["id"]]
    stored = _get(job["id"])
    assert stored["repeat"]["completed"] == 1
    assert stored["state"] == "completed" and stored["last_status"] == "ok"


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
# Without HERMES_DWD_IDENTITY the principal is still only for the operator
#
# HERMES_DWD_IDENTITY is set only for a governed non-admin under enforce. A
# terminal of a WebUI session under report_only, of a gateway or cron run, of
# a kanban worker or of a dashboard-started run has none, but it is still not
# the operator's own shell: `hermes cron create` there must not produce an
# administrator-owned job. It stays ownerless (refused under enforce) until an
# administrator assigns an owner.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, value",
    [
        ("HERMES_SESSION_PLATFORM", "webui"),
        ("HERMES_SESSION_USER_ID", "mallory@example.test"),
        ("HERMES_SESSION_KEY", "webui-session-1"),
        ("HERMES_SESSION_ID", "20260927_101010_abcdef"),
        ("HERMES_SESSION_CHAT_ID", "chat-1"),
        ("HERMES_SESSION_SOURCE", "desktop"),
        ("HERMES_UI_SESSION_ID", "ui-1"),
        ("HERMES_CRON_SESSION", "1"),
        ("HERMES_CRON_JOB_ID", "abc123"),
        ("HERMES_GATEWAY_SESSION", "1"),
        ("_HERMES_GATEWAY", "1"),
        ("HERMES_KANBAN_TASK", "t_1"),
    ],
)
def test_the_cli_scope_does_not_stamp_the_principal_inside_a_session(monkeypatch, name, value):
    from cron.jobs import system_principal_create_scope

    _write_config(PRINCIPAL)
    monkeypatch.setenv(name, value)
    with system_principal_create_scope():
        assert _job()["owner_email"] == ""


@pytest.mark.parametrize("value", ["", "  ", "0", "false"])
def test_an_empty_or_off_session_flag_is_not_a_session(monkeypatch, value):
    from cron.jobs import system_principal_create_scope

    _write_config(PRINCIPAL)
    monkeypatch.setenv("HERMES_CRON_SESSION", value)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", value)
    with system_principal_create_scope():
        assert _job()["owner_email"] == PRINCIPAL


def test_the_cli_scope_does_not_stamp_the_principal_under_a_dashboard_run(monkeypatch):
    """A dashboard-started run hands its governance to children in the
    environment. Even an administrator's run is not the operator's shell."""
    from cron.jobs import system_principal_create_scope
    from hermes_cli.dashboard_governance.context import (
        GOVERNANCE_CONTEXT_ENV,
        serialize_context_for_env,
    )

    _write_config(PRINCIPAL)
    monkeypatch.setenv(
        GOVERNANCE_CONTEXT_ENV,
        serialize_context_for_env(_governed("root@example.test", admin=True)),
    )
    with system_principal_create_scope():
        assert _job()["owner_email"] == ""


@pytest.mark.parametrize("mode", ["report_only", "enforce"])
def test_the_cli_scope_does_not_stamp_the_principal_under_a_bound_context(mode):
    from cron.jobs import system_principal_create_scope
    from hermes_cli.dashboard_governance.context import governance_context

    _write_config(PRINCIPAL)
    with system_principal_create_scope(), governance_context(_governed("mallory@example.test", mode="report_only")):
        assert _job()["owner_email"] == ""
    with system_principal_create_scope(), governance_context(_governed("root@example.test", admin=True, mode=mode)):
        owner = _job()["owner_email"]
    assert owner == "", "the CLI scope is for the operator's shell only"


def test_the_cli_scope_does_not_stamp_the_principal_for_a_bridged_session(monkeypatch):
    """A host that binds session variables in-process (gateway, API server)
    is not the operator's shell either."""
    import contextvars

    import gateway.session_context as session_context
    from cron.jobs import system_principal_create_scope

    _write_config(PRINCIPAL)
    # set_session_vars flips a process-wide flag; restore it after the test.
    monkeypatch.setattr(session_context, "_session_context_engaged", session_context._session_context_engaged)

    def create_in_a_bound_session():
        session_context.set_session_vars(platform="telegram", chat_id="42", user_id="7")
        with system_principal_create_scope():
            return _job()["owner_email"]

    assert contextvars.copy_context().run(create_in_a_bound_session) == ""


def test_hermes_cron_create_in_a_webui_terminal_is_not_the_principal(monkeypatch, capsys):
    """End to end: a WebUI terminal under report_only (no DWD identity)."""
    from cron.jobs import list_jobs

    _write_config(PRINCIPAL)
    _write_policy("report_only", users={MALLORY: {"roles": ["tech_lead"]}})
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", MALLORY)

    assert _cli(["cron", "create", "every 1h", "Read every mailbox", "--name", "webui shell job"]) == 0
    (job,) = [j for j in list_jobs(include_disabled=True) if j["name"] == "webui shell job"]
    assert job["owner_email"] == ""


# ---------------------------------------------------------------------------
# Named profiles follow the platform policy
#
# Only the root config.yaml points at the platform policy. A named profile
# that sets no policy of its own must not read as "governance off", or its
# ownerless agent jobs run unbound while the platform is in enforce.
# ---------------------------------------------------------------------------


def _platform(tmp_policy_location="configured", *, mode="enforce", principal=PRINCIPAL):
    """The root home as production has it: config.yaml points at the policy."""
    root = _home()
    policy = root / "dashboard-governance.yaml"
    data = {
        "version": 1,
        "mode": mode,
        "default_effect": "deny",
        "bootstrap_admins": [PRINCIPAL],
        "roles": {"tech_lead": {"grants": {"tools": ["terminal"]}}},
        "users": {"alice@example.test": {"roles": ["tech_lead"]}},
    }
    policy.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = {"model": "test-model"}
    if principal:
        cfg["cron"] = {"system_principal": principal}
    if tmp_policy_location == "configured":
        cfg["dashboard"] = {"governance": {"policy_file": str(policy)}}
    (root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return root


class _Profile:
    """Scope the active home and the cron store to a named profile."""

    def __init__(self, root, name="worker", *, config=None, own_policy_mode=None):
        self.home = root / "profiles" / name
        (self.home / "cron").mkdir(parents=True, exist_ok=True)
        cfg = dict(config or {"model": "test-model"})
        if own_policy_mode is not None:
            policy = self.home / "dashboard-governance.yaml"
            policy.write_text(
                yaml.safe_dump({"version": 1, "mode": own_policy_mode, "default_effect": "deny"}),
                encoding="utf-8",
            )
        (self.home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    def __enter__(self):
        from cron.jobs import use_cron_store
        from hermes_constants import set_hermes_home_override

        self._token = set_hermes_home_override(str(self.home))
        self._store = use_cron_store(self.home)
        self._store.__enter__()
        return self.home

    def __exit__(self, *exc):
        from hermes_constants import reset_hermes_home_override

        self._store.__exit__(*exc)
        reset_hermes_home_override(self._token)
        return False


def _patch_run_with_mode(monkeypatch):
    calls = []

    def fake_run_job(job, **_kw):
        from hermes_cli.dashboard_governance.context import current_governance_context

        ctx = current_governance_context()
        calls.append((job["id"], ctx.access.subject.email if ctx else None, ctx.access.mode if ctx else None))
        return (True, "output", "final response", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "_deliver_result", lambda *_a, **_kw: None)
    return calls


@pytest.mark.parametrize("location", ["configured", "default"])
def test_a_profile_store_without_its_own_policy_follows_the_platform(monkeypatch, location):
    root = _platform(location)
    calls = _patch_run_with_mode(monkeypatch)
    with _Profile(root) as profile:
        job = _job()
        assert job["owner_email"] == ""
        assert s.run_one_job(_get(job["id"])) is True
        stored = _get(job["id"])
    assert calls == []
    assert stored["last_status"] == "blocked_config"
    assert "no owner" in stored["last_error"]
    data = json.loads((profile / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert [j["id"] for j in data["jobs"]] == [job["id"]], "the refusal lands in the profile store"


def test_an_owned_job_in_a_profile_store_runs_under_the_platform_policy(monkeypatch):
    root = _platform()
    calls = _patch_run_with_mode(monkeypatch)
    with _Profile(root):
        job = _job(owner_email="alice@example.test")
        assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], "alice@example.test", "enforce")]


def test_a_profile_with_its_own_policy_keeps_it(monkeypatch):
    root = _platform()
    calls = _patch_run_with_mode(monkeypatch)
    with _Profile(root, own_policy_mode="off"):
        job = _job()
        assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], None, None)]


def test_a_profile_that_names_a_policy_file_keeps_it(monkeypatch, tmp_path):
    root = _platform()
    own = tmp_path / "profile-policy.yaml"
    own.write_text(yaml.safe_dump({"version": 1, "mode": "report_only", "default_effect": "deny"}), encoding="utf-8")
    calls = _patch_run_with_mode(monkeypatch)
    cfg = {"model": "test-model", "dashboard": {"governance": {"policy_file": str(own)}}}
    with _Profile(root, config=cfg):
        job = _job()
        assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], None, None)]


@pytest.mark.parametrize("platform_mode", ["enforce", "off", None])
def test_a_profile_that_names_a_missing_policy_file_fails_closed(monkeypatch, tmp_path, platform_mode):
    """The loader reads a missing policy file as governance "off". For a
    profile that names one (a typo, a moved file) that would run its ownerless
    agent jobs unbound, so it fails closed instead, whatever the platform's
    mode, and an owned job is refused too because its owner's grants cannot be
    resolved."""
    from cron.jobs import resolve_cron_policy_path

    if platform_mode is None:
        root = _home()
        _write_config(PRINCIPAL)
    else:
        root = _platform(mode=platform_mode)
    calls = _patch_run_with_mode(monkeypatch)
    cfg = {"model": "test-model", "dashboard": {"governance": {"policy_file": str(tmp_path / "gone.yaml")}}}
    with _Profile(root, config=cfg) as profile:
        with pytest.raises(ValueError, match="does not exist"):
            resolve_cron_policy_path(hermes_home=profile, config=cfg)
        ownerless = _job()
        owned = _job(owner_email="alice@example.test")
        script = _job(prompt="", script="watchdog.sh", no_agent=True)
        for job in (ownerless, owned, script):
            assert s.run_one_job(_get(job["id"])) is True
        stored = {job["id"]: _get(job["id"]) for job in (ownerless, owned)}
    assert calls == [(script["id"], None, None)], "only the ownerless script still runs"
    for record in stored.values():
        assert record["last_status"] == "blocked_config"
        assert "policy" in record["last_error"]


def test_a_root_home_that_names_a_missing_policy_file_keeps_the_loader_rule(monkeypatch, tmp_path):
    """The root store is the platform: its missing policy is "off" for the
    WebUI and the dashboard as well, so cron follows the same rule there."""
    from cron.jobs import resolve_cron_policy_path

    cfg = {"model": "test-model", "dashboard": {"governance": {"policy_file": str(tmp_path / "gone.yaml")}}}
    (_home() / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert resolve_cron_policy_path() == (tmp_path / "gone.yaml", "store")
    calls = _patch_run(monkeypatch)
    job = _job()
    assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], None)]


def test_a_profile_store_runs_as_before_when_the_platform_has_no_policy(monkeypatch):
    root = _home()
    _write_config(PRINCIPAL)
    calls = _patch_run_with_mode(monkeypatch)
    with _Profile(root):
        job = _job()
        assert s.run_one_job(_get(job["id"])) is True
    assert calls == [(job["id"], None, None)]


def test_an_unreadable_platform_config_refuses_an_ownerless_profile_job(monkeypatch):
    root = _platform()
    (root / "config.yaml").write_text("dashboard: [governance\n", encoding="utf-8")
    calls = _patch_run_with_mode(monkeypatch)
    with _Profile(root):
        job = _job()
        assert s.run_one_job(_get(job["id"])) is True
        assert "policy" in _get(job["id"])["last_error"]
    assert calls == []


def test_a_cli_create_in_a_profile_gets_the_platform_principal():
    from cron.jobs import cron_system_principal, system_principal_create_scope

    root = _platform()
    with _Profile(root):
        assert cron_system_principal() == PRINCIPAL
        with system_principal_create_scope():
            assert _job()["owner_email"] == PRINCIPAL


def test_a_profile_principal_wins_over_the_platform_one():
    from cron.jobs import cron_system_principal

    root = _platform()
    cfg = {"model": "test-model", "cron": {"system_principal": "ops@example.test"}}
    with _Profile(root, config=cfg):
        assert cron_system_principal() == "ops@example.test"


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


def test_the_migration_reads_the_platform_policy_for_profile_stores(capsys):
    """Profile configs do not name the policy; the script must still see the
    platform's enforce mode and block when the principal has no entry."""
    script = _load_script()
    root = _platform(principal=PRINCIPAL)
    policy = yaml.safe_load((root / "dashboard-governance.yaml").read_text(encoding="utf-8"))
    policy["bootstrap_admins"] = []
    (root / "dashboard-governance.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    profile = _Profile(root).home
    (profile / "cron" / "jobs.json").write_text(json.dumps({"jobs": [_raw_job("agent2")]}), encoding="utf-8")

    assert script.main(["--json", "--hermes-home", str(profile)]) == 1
    (store,) = json.loads(capsys.readouterr().out)["stores"]
    assert store["governance_mode"] == "enforce"
    assert store["policy_file"] == str(root / "dashboard-governance.yaml")
    assert store["principal"] == PRINCIPAL
    assert any("no entry" in error for error in store["errors"])

    assert script.main(["--apply", "--hermes-home", str(profile)]) == 1
    assert _owners(profile)["agent2"] == ""


def test_the_migration_assigns_the_platform_principal_in_profile_stores(capsys):
    script = _load_script()
    root = _platform(principal=PRINCIPAL)
    profile = _Profile(root).home
    (profile / "cron" / "jobs.json").write_text(json.dumps({"jobs": [_raw_job("agent2")]}), encoding="utf-8")

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert _owners(profile)["agent2"] == PRINCIPAL


def test_the_migration_blocks_a_profile_whose_named_policy_is_missing(capsys, tmp_path):
    """A profile that points at a policy file that does not exist reads as
    governance off. With the platform in enforce that is a hole, not a mode."""
    script = _load_script()
    root = _platform(principal=PRINCIPAL)
    cfg = {"model": "test-model", "dashboard": {"governance": {"policy_file": str(tmp_path / "gone.yaml")}}}
    profile = _Profile(root, config=cfg).home
    (profile / "cron" / "jobs.json").write_text(json.dumps({"jobs": [_raw_job("agent2")]}), encoding="utf-8")

    assert script.main(["--hermes-home", str(profile)]) == 1
    assert "does not exist" in capsys.readouterr().out
    assert script.main(["--apply", "--hermes-home", str(profile)]) == 1
    assert _owners(profile)["agent2"] == ""


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


# ---------------------------------------------------------------------------
# The migration never reports all clear while a store has ownerless agent jobs
# and never gives a person's own profile to the administrator principal
# ---------------------------------------------------------------------------


def test_the_migration_reports_profile_stores_even_without_all_profiles(capsys):
    """The root store is clean, a profile store is not: without
    --all-profiles the script must say so and must not exit 0."""
    from cron.jobs import create_job

    script = _load_script()
    _write_config(PRINCIPAL)
    _write_policy("enforce")
    create_job(prompt="owned", schedule="every 1h", owner_email="alice@example.test")
    profile = _make_store(
        _home() / "profiles" / "worker",
        [_raw_job("agent2"), _raw_job("script2", no_agent=True), _raw_job("owned2", owner="alice@example.test")],
    )

    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert str(profile) in out and "agent2" in out and "--all-profiles" in out
    assert "script2" not in out, "a script job is not refused, so it does not hold up the all clear"
    assert "All clear" not in out

    assert script.main(["--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert [s_["home"] for s_ in report["stores"]] == [str(_home().resolve())]
    (uncovered,) = report["uncovered_stores"]
    assert uncovered["home"] == str(profile.resolve())
    assert uncovered["ownerless_agent_jobs"] == ["agent2"]

    assert script.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "Not all clear" in out and "All clear" not in out
    assert _owners(profile)["agent2"] == "", "a store that was not selected is not changed"

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert "All clear" in capsys.readouterr().out
    assert _owners(profile)["agent2"] == PRINCIPAL


def test_the_migration_checks_the_whole_platform_when_given_one_profile(capsys):
    from cron.jobs import create_job

    script = _load_script()
    _write_config(PRINCIPAL)
    _write_policy("enforce")
    left = create_job(prompt="root legacy", schedule="every 1h")
    profile = _make_store(_home() / "profiles" / "worker", [_raw_job("agent2")])

    assert script.main(["--apply", "--hermes-home", str(profile)]) == 1
    out = capsys.readouterr().out
    assert _owners(profile)["agent2"] == PRINCIPAL
    assert left["id"] in out and "Not all clear" in out


def _personal_platform(people):
    """The platform policy gives the profile "mallory" to ``people``."""
    root = _platform()
    policy = yaml.safe_load((root / "dashboard-governance.yaml").read_text(encoding="utf-8"))
    policy["users"] = {
        email: {"roles": ["tech_lead"], "grants": {"profiles": ["mallory"]}} for email in people
    }
    policy["users"]["alice@example.test"] = {"roles": ["tech_lead"], "grants": {"profiles": ["*"]}}
    (root / "dashboard-governance.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    profile = _Profile(root, "mallory").home
    (profile / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": [_raw_job("agent3"), _raw_job("script3", no_agent=True)]}), encoding="utf-8"
    )
    return root, profile


def test_the_migration_gives_a_personal_profile_job_to_its_person(capsys):
    """The administrator principal in a governed person's own profile would
    run their jobs with administrator rights. The person owns them instead."""
    from cron.jobs import OWNER_AUDIT_FILE_NAME

    script = _load_script()
    _root, profile = _personal_platform([MALLORY])

    assert script.main(["--json", "--all-profiles"]) == 0
    report = json.loads(capsys.readouterr().out)
    (store,) = [s_ for s_ in report["stores"] if s_["home"] == str(profile.resolve())]
    assert store["profile_people"] == [MALLORY]
    assert {(j["id"], j["action"], j["new_owner"]) for j in store["jobs"]} == {
        ("agent3", "would_assign", MALLORY),
        ("script3", "would_assign", MALLORY),
    }

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert "All clear" in capsys.readouterr().out
    assert _owners(profile) == {"agent3": MALLORY, "script3": MALLORY}
    rows = [json.loads(line) for line in (profile / "cron" / OWNER_AUDIT_FILE_NAME).read_text().splitlines()]
    assert {r["new_owner"] for r in rows} == {MALLORY}
    assert all(r["new_owner"] != PRINCIPAL for r in rows)


def test_the_migration_leaves_a_profile_that_maps_to_several_people(capsys):
    script = _load_script()
    _root, profile = _personal_platform([MALLORY, "bob@example.test"])

    assert script.main(["--all-profiles"]) == 1
    out = capsys.readouterr().out
    assert "agent3" in out and "several people" in out

    assert script.main(["--apply", "--all-profiles"]) == 1
    out = capsys.readouterr().out
    assert "Not all clear" in out and "All clear" not in out
    assert _owners(profile) == {"agent3": "", "script3": ""}


def test_a_profile_named_only_for_administrators_gets_the_principal(capsys):
    script = _load_script()
    root, profile = _personal_platform([])
    policy = yaml.safe_load((root / "dashboard-governance.yaml").read_text(encoding="utf-8"))
    policy["users"]["root@example.test"] = {"roles": ["admin"], "grants": {"profiles": ["mallory"]}}
    policy["bootstrap_admins"] = [PRINCIPAL, "root@example.test"]
    (root / "dashboard-governance.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert _owners(profile) == {"agent3": PRINCIPAL, "script3": PRINCIPAL}


def test_the_explicit_principal_never_overrides_a_personal_profile(capsys):
    script = _load_script()
    _root, profile = _personal_platform([MALLORY])

    assert script.main(["--apply", "--all-profiles", "--principal", PRINCIPAL]) == 0
    assert _owners(profile) == {"agent3": MALLORY, "script3": MALLORY}


def test_a_profile_with_its_own_policy_still_belongs_to_its_person(capsys):
    """The people are defined in the platform policy; a profile's own policy
    (here "off", with nobody in it) does not hide whose profile it is."""
    script = _load_script()
    _root, profile = _personal_platform([MALLORY])
    (profile / "dashboard-governance.yaml").write_text(
        yaml.safe_dump({"version": 1, "mode": "off", "default_effect": "deny"}), encoding="utf-8"
    )

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert _owners(profile) == {"agent3": MALLORY, "script3": MALLORY}
