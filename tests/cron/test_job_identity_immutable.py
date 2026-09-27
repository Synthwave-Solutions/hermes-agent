"""A cron job's identity cannot be changed through an ordinary edit.

``owner_email`` decides whose governance a fire runs under, ``origin`` where
"origin" delivery goes and ``created_at`` when the job was made. Before this,
``update_job`` protected only ``id``, so anyone who could edit a job (the agent
``cronjob`` tool, the engine dashboard, the API) could blank the owner and make
the job run with nobody's governance, or re-own it to someone else.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

IDENTITY_FIELDS = ("owner_email", "origin", "created_at")
ORIGIN = {"platform": "telegram", "chat_id": "111", "user_id": "alice"}


@pytest.fixture()
def owned_job():
    from cron.jobs import create_job

    return create_job(
        prompt="Summarise the inbox",
        schedule="every 1h",
        name="inbox summary",
        origin=dict(ORIGIN),
        owner_email="alice@example.test",
    )


def _stored(job_id):
    from cron.jobs import load_jobs

    return next(j for j in load_jobs() if j["id"] == job_id)


# ---------------------------------------------------------------------------
# update_job: the chokepoint every edit path goes through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "updates",
    [
        {"owner_email": ""},
        {"owner_email": None},
        {"owner_email": "mallory@example.test"},
        {"origin": None},
        {"origin": {"platform": "telegram", "chat_id": "999"}},
        {"created_at": "2020-01-01T00:00:00+00:00"},
    ],
)
def test_update_job_refuses_identity_fields(owned_job, updates):
    from cron.jobs import update_job

    with pytest.raises(ValueError, match="cannot be updated"):
        update_job(owned_job["id"], updates)

    stored = _stored(owned_job["id"])
    assert stored["owner_email"] == "alice@example.test"
    assert stored["origin"] == ORIGIN
    assert stored["created_at"] == owned_job["created_at"]


def test_a_refused_identity_edit_applies_nothing(owned_job):
    """A mixed payload is refused whole: the benign part does not sneak in."""
    from cron.jobs import update_job

    with pytest.raises(ValueError, match="owner_email"):
        update_job(owned_job["id"], {"name": "renamed", "owner_email": ""})

    stored = _stored(owned_job["id"])
    assert stored["name"] == "inbox summary"
    assert stored["owner_email"] == "alice@example.test"


def test_every_identity_field_is_named_in_the_refusal(owned_job):
    from cron.jobs import update_job

    with pytest.raises(ValueError) as exc:
        update_job(owned_job["id"], {k: None for k in IDENTITY_FIELDS})
    for field in IDENTITY_FIELDS:
        assert field in str(exc.value)


def test_id_stays_immutable(owned_job):
    from cron.jobs import update_job

    with pytest.raises(ValueError, match="id"):
        update_job(owned_job["id"], {"id": "../escape"})


def test_ordinary_edits_keep_working_and_keep_the_identity(owned_job):
    from cron.jobs import update_job

    updated = update_job(owned_job["id"], {"name": "renamed", "schedule": "every 2h"})

    assert updated["name"] == "renamed"
    assert updated["owner_email"] == "alice@example.test"
    assert updated["origin"] == ORIGIN
    assert updated["created_at"] == owned_job["created_at"]


def test_lifecycle_helpers_do_not_touch_the_identity(owned_job):
    """pause/resume/trigger go through update_job and must keep working."""
    from cron.jobs import pause_job, resume_job, trigger_job

    assert pause_job(owned_job["id"])["state"] == "paused"
    assert resume_job(owned_job["id"])["enabled"] is True
    assert trigger_job(owned_job["id"]) is not None
    stored = _stored(owned_job["id"])
    assert stored["owner_email"] == "alice@example.test"
    assert stored["origin"] == ORIGIN


# ---------------------------------------------------------------------------
# The WebUI Tasks panel create flow
#
# These two tests pin a go-live coupling. WebUI builds before the cron write
# fix ("fix(cron): stamp owner and origin when a task is created") create the
# job first and then stamp the creator with
# ``update_job(job_id, {..., "origin": {"platform": "webui", ...}})``. This
# engine refuses that update after the job is already saved, so the Tasks
# panel answers 400 and leaves an enabled, ownerless job on the default
# profile. Never take this engine live before (or without) a WebUI that
# passes ``owner_email`` and ``origin`` to ``create_job``. Do not make
# ``origin`` writable to make the old flow pass: settable once is still a
# way to re-target where "origin" delivery goes.
# ---------------------------------------------------------------------------

WEBUI_CREATOR = "alice@example.test"
WEBUI_ORIGIN = {"platform": "webui", "chat_id": None, "user_id": WEBUI_CREATOR}


def test_the_legacy_webui_post_create_origin_stamp_is_refused():
    from cron.jobs import create_job, update_job

    job = create_job(prompt="Summarise the inbox", schedule="every 1h", deliver="local")
    post_create_updates = {
        "category": "Reporting",
        "emoji": "\U0001F4EC",
        "toast_notifications": False,
        "origin": dict(WEBUI_ORIGIN),
    }

    with pytest.raises(ValueError, match="cannot be updated: origin"):
        update_job(job["id"], post_create_updates)

    stored = _stored(job["id"])
    assert stored.get("origin") is None
    assert stored["owner_email"] == ""
    assert "category" not in stored, "the refused update applies nothing"


def test_the_webui_create_shape_keeps_the_identity_through_its_follow_up_update():
    from cron.jobs import create_job, update_job

    job = create_job(
        prompt="Summarise the inbox",
        schedule="every 1h",
        deliver="local",
        origin=dict(WEBUI_ORIGIN),
        owner_email=WEBUI_CREATOR,
    )
    updated = update_job(
        job["id"],
        {
            "category": "Reporting",
            "emoji": "\U0001F4EC",
            "toast_notifications": False,
            "diagram": "flowchart LR\n  A --> B",
            "shared_with": ["bob@example.test"],
        },
    )

    assert updated["owner_email"] == WEBUI_CREATOR
    assert updated["origin"] == WEBUI_ORIGIN
    assert updated["category"] == "Reporting"
    assert updated["shared_with"] == ["bob@example.test"]


# ---------------------------------------------------------------------------
# The agent ``cronjob`` tool
# ---------------------------------------------------------------------------


def test_cronjob_tool_schema_offers_no_identity_fields():
    from tools.cronjob_tools import CRONJOB_SCHEMA

    properties = CRONJOB_SCHEMA["parameters"]["properties"]
    for field in IDENTITY_FIELDS:
        assert field not in properties


def test_cronjob_tool_update_cannot_change_the_identity(owned_job):
    """The model may send extra keys; the handler drops them and the edit it
    is allowed to make still lands."""
    from tools.cronjob_tools import _cronjob_handler

    result = json.loads(
        _cronjob_handler(
            {
                "action": "update",
                "job_id": owned_job["id"],
                "name": "renamed by the agent",
                "owner_email": "",
                "origin": {"platform": "telegram", "chat_id": "999"},
                "created_at": "2020-01-01T00:00:00+00:00",
            }
        )
    )

    assert result["success"] is True
    stored = _stored(owned_job["id"])
    assert stored["name"] == "renamed by the agent"
    assert stored["owner_email"] == "alice@example.test"
    assert stored["origin"] == ORIGIN
    assert stored["created_at"] == owned_job["created_at"]


def test_cronjob_function_has_no_identity_parameters():
    import inspect

    from tools.cronjob_tools import cronjob

    params = inspect.signature(cronjob).parameters
    for field in IDENTITY_FIELDS:
        assert field not in params


# ---------------------------------------------------------------------------
# The engine dashboard (``CronJobUpdate`` carries a free-form dict)
# ---------------------------------------------------------------------------


@pytest.fixture()
def dashboard_profile(tmp_path, monkeypatch):
    """An isolated default profile home the dashboard cron routes resolve to."""
    from hermes_cli import profiles

    default_home = tmp_path / ".hermes"
    (default_home / "cron").mkdir(parents=True)
    (default_home / "config.yaml").write_text("model: test-model\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: default_home / "profiles")
    return default_home


@pytest.mark.parametrize(
    "updates",
    [
        {"owner_email": ""},
        {"owner_email": "mallory@example.test"},
        {"origin": {"platform": "telegram", "chat_id": "999"}},
        {"created_at": "2020-01-01T00:00:00+00:00"},
        {"name": "renamed", "owner_email": ""},
    ],
)
def test_dashboard_update_refuses_identity_fields(dashboard_profile, monkeypatch, updates):
    from hermes_cli import web_server

    notified = []
    monkeypatch.setattr(web_server, "_notify_cron_provider_for_profile", notified.append)
    job = web_server._call_cron_for_profile(
        "default",
        "create_job",
        prompt="Summarise the inbox",
        schedule="every 1h",
        name="inbox summary",
        origin=dict(ORIGIN),
        owner_email="alice@example.test",
    )

    with pytest.raises(HTTPException) as exc:
        web_server._update_cron_job_sync(
            job["id"], web_server.CronJobUpdate(updates=updates), profile="default"
        )

    assert exc.value.status_code == 400
    assert "cannot be updated" in exc.value.detail
    assert notified == []
    stored = web_server._call_cron_for_profile("default", "get_job", job["id"])
    assert stored["name"] == "inbox summary"
    assert stored["owner_email"] == "alice@example.test"
    assert stored["origin"] == ORIGIN
    assert stored["created_at"] == job["created_at"]


# ---------------------------------------------------------------------------
# The admin path: reassign_job_owner / ``hermes cron reassign-owner``
# ---------------------------------------------------------------------------


def _audit_rows():
    from cron.jobs import owner_audit_file

    path = owner_audit_file()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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


def test_reassign_changes_only_the_owner_and_is_audited(owned_job):
    from cron.jobs import reassign_job_owner

    result = reassign_job_owner(
        owned_job["id"], " Bob@Example.Test ", actor="os:tester", reason="handover"
    )

    assert result["changed"] is True
    assert result["previous_owner"] == "alice@example.test"
    assert result["new_owner"] == "bob@example.test"
    stored = _stored(owned_job["id"])
    assert stored["owner_email"] == "bob@example.test"
    assert stored["origin"] == ORIGIN
    assert stored["created_at"] == owned_job["created_at"]
    rows = _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "cron_owner_reassigned"
    assert row["job_id"] == owned_job["id"]
    assert row["job_name"] == "inbox summary"
    assert row["previous_owner"] == "alice@example.test"
    assert row["new_owner"] == "bob@example.test"
    assert row["actor"] == "os:tester"
    assert row["reason"] == "handover"
    assert row["source"] == "cli"
    assert row["ts"]


def test_reassign_accepts_a_job_name(owned_job):
    from cron.jobs import reassign_job_owner

    result = reassign_job_owner("inbox summary", "bob@example.test", actor="os:tester")
    assert result["job"]["id"] == owned_job["id"]
    assert _stored(owned_job["id"])["owner_email"] == "bob@example.test"


@pytest.mark.parametrize("owner", ["", "   ", None, "not-an-address", "a b@example.test", "a@b@c"])
def test_reassign_refuses_an_invalid_or_empty_owner(owned_job, owner):
    """There is no admin path to an ownerless job either: a job is re-owned,
    never un-owned."""
    from cron.jobs import reassign_job_owner

    with pytest.raises(ValueError):
        reassign_job_owner(owned_job["id"], owner, actor="os:tester")
    assert _stored(owned_job["id"])["owner_email"] == "alice@example.test"
    assert _audit_rows() == []


def test_reassign_requires_an_actor(owned_job):
    from cron.jobs import reassign_job_owner

    with pytest.raises(ValueError):
        reassign_job_owner(owned_job["id"], "bob@example.test", actor="  ")
    assert _stored(owned_job["id"])["owner_email"] == "alice@example.test"


def test_reassign_of_a_missing_job_returns_none():
    from cron.jobs import reassign_job_owner

    assert reassign_job_owner("nope", "bob@example.test", actor="os:tester") is None
    assert _audit_rows() == []


def test_reassign_to_the_same_owner_is_a_no_op(owned_job):
    from cron.jobs import reassign_job_owner

    result = reassign_job_owner(owned_job["id"], "ALICE@example.test", actor="os:tester")
    assert result["changed"] is False
    assert _audit_rows() == []


def test_reassign_with_a_stale_expected_owner_changes_nothing(owned_job):
    from cron.jobs import reassign_job_owner

    with pytest.raises(ValueError, match="owner"):
        reassign_job_owner(
            owned_job["id"], "bob@example.test", actor="os:tester", expected_owner=""
        )
    assert _stored(owned_job["id"])["owner_email"] == "alice@example.test"
    assert _audit_rows() == []


def test_reassign_gives_an_ownerless_job_an_owner():
    from cron.jobs import create_job, reassign_job_owner

    job = create_job(prompt="legacy", schedule="every 1h")
    assert job["owner_email"] == ""

    result = reassign_job_owner(job["id"], "ops@example.test", actor="os:tester", expected_owner="")
    assert result["changed"] is True
    assert _stored(job["id"])["owner_email"] == "ops@example.test"


def test_reassign_is_refused_from_a_governed_shell(owned_job, monkeypatch):
    """A governed, non-admin person's terminal carries HERMES_DWD_IDENTITY.
    They must not be able to re-own a job (to themselves or anyone else)."""
    from cron.jobs import reassign_job_owner

    monkeypatch.setenv("HERMES_DWD_IDENTITY", "mallory@example.test")
    with pytest.raises(PermissionError):
        reassign_job_owner(owned_job["id"], "mallory@example.test", actor="os:tester")
    assert _stored(owned_job["id"])["owner_email"] == "alice@example.test"
    assert _audit_rows() == []


def test_reassign_is_refused_in_a_governed_non_admin_session(owned_job):
    from cron.jobs import reassign_job_owner
    from hermes_cli.dashboard_governance.context import governance_context

    with governance_context(_governed("mallory@example.test")):
        with pytest.raises(PermissionError):
            reassign_job_owner(owned_job["id"], "mallory@example.test", actor="os:tester")
    assert _stored(owned_job["id"])["owner_email"] == "alice@example.test"


def test_reassign_is_allowed_in_a_governed_admin_session(owned_job):
    from cron.jobs import reassign_job_owner
    from hermes_cli.dashboard_governance.context import governance_context

    with governance_context(_governed("root@example.test", admin=True)):
        result = reassign_job_owner(owned_job["id"], "bob@example.test", actor="root@example.test")
    assert result["changed"] is True


def test_a_failed_audit_write_leaves_no_unaudited_change(owned_job, monkeypatch):
    import cron.jobs as jobs_mod

    def _boom(_row):
        raise OSError("disk full")

    monkeypatch.setattr(jobs_mod, "_append_owner_audit", _boom)
    with pytest.raises(OSError):
        jobs_mod.reassign_job_owner(owned_job["id"], "bob@example.test", actor="os:tester")
    assert _stored(owned_job["id"])["owner_email"] == "alice@example.test"


def test_the_owner_audit_log_is_private(owned_job):
    import stat

    from cron.jobs import owner_audit_file, reassign_job_owner

    reassign_job_owner(owned_job["id"], "bob@example.test", actor="os:tester")
    assert stat.S_IMODE(owner_audit_file().stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# CLI: hermes cron reassign-owner
# ---------------------------------------------------------------------------


def _sentinel_cmd_cron(args):  # pragma: no cover - must never be reached
    raise AssertionError("reassign-owner must not dispatch through cmd_cron")


def _parse(argv):
    import argparse

    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=_sentinel_cmd_cron)
    return parser.parse_args(argv)


def _write_config(text):
    from hermes_constants import get_hermes_home

    (get_hermes_home() / "config.yaml").write_text(text, encoding="utf-8")


def test_cli_reassign_owner(owned_job, capsys):
    args = _parse(["cron", "reassign-owner", owned_job["id"], "bob@example.test", "--reason", "handover"])

    assert args.func(args) == 0
    assert _stored(owned_job["id"])["owner_email"] == "bob@example.test"
    out = capsys.readouterr().out
    assert "alice@example.test" in out and "bob@example.test" in out
    rows = _audit_rows()
    assert rows[-1]["reason"] == "handover"
    assert rows[-1]["source"] == "cli"
    assert rows[-1]["actor"].startswith("os:")


def test_cli_reassign_owner_to_the_system_principal(owned_job):
    _write_config("cron:\n  system_principal: Cron-System@Example.Test\n")
    args = _parse(["cron", "reassign-owner", owned_job["id"], "--system-principal"])

    assert args.func(args) == 0
    assert _stored(owned_job["id"])["owner_email"] == "cron-system@example.test"


def test_cli_reassign_owner_without_a_configured_system_principal(owned_job, capsys):
    args = _parse(["cron", "reassign-owner", owned_job["id"], "--system-principal"])

    assert args.func(args) == 1
    assert "system_principal" in capsys.readouterr().out
    assert _stored(owned_job["id"])["owner_email"] == "alice@example.test"


@pytest.mark.parametrize(
    "extra",
    [[], ["bob@example.test", "--system-principal"]],
)
def test_cli_reassign_owner_needs_exactly_one_owner(owned_job, extra):
    args = _parse(["cron", "reassign-owner", owned_job["id"], *extra])

    assert args.func(args) == 2
    assert _stored(owned_job["id"])["owner_email"] == "alice@example.test"


def test_cli_reassign_owner_reports_errors(owned_job, capsys, monkeypatch):
    args = _parse(["cron", "reassign-owner", "no-such-job", "bob@example.test"])
    assert args.func(args) == 1
    assert "not found" in capsys.readouterr().out.lower()

    args = _parse(["cron", "reassign-owner", owned_job["id"], "not-an-address"])
    assert args.func(args) == 1

    monkeypatch.setenv("HERMES_DWD_IDENTITY", "mallory@example.test")
    args = _parse(["cron", "reassign-owner", owned_job["id"], "mallory@example.test"])
    assert args.func(args) == 1
    assert _stored(owned_job["id"])["owner_email"] == "alice@example.test"


def test_cli_reassign_owner_reports_an_ambiguous_name(capsys):
    from cron.jobs import create_job

    create_job(prompt="a", schedule="every 1h", name="twin", owner_email="alice@example.test")
    create_job(prompt="b", schedule="every 1h", name="twin", owner_email="alice@example.test")
    args = _parse(["cron", "reassign-owner", "twin", "bob@example.test"])

    assert args.func(args) == 1
    assert "ambiguous" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Only the owner or an administrator acts on a job
#
# A governed person who is not an administrator (under enforce: an in-process
# governance context, or a shell that carries HERMES_DWD_IDENTITY) could list
# every job and edit, re-target, pause, resume, remove or run any of them,
# including the jobs of the system principal, which run with administrator
# rights. They now see and act on their own jobs only. Ownerless and
# principal-owned jobs are for administrators.
# ---------------------------------------------------------------------------

PRINCIPAL = "cron-system@example.test"
MALLORY = "mallory@example.test"


@pytest.fixture()
def foreign_jobs():
    """The principal's job, alice's job, a legacy ownerless job and one of mallory's."""
    from cron.jobs import _jobs_lock, create_job, load_jobs, save_jobs

    jobs = {
        "principal": create_job(prompt="Daily ops summary", schedule="every 1h", name="ops", owner_email=PRINCIPAL),
        "alice": create_job(prompt="Alice inbox", schedule="every 1h", name="alice", owner_email="alice@example.test"),
        "ownerless": create_job(prompt="Legacy", schedule="every 1h", name="legacy", owner_email="x@example.test"),
        "mine": create_job(prompt="Mallory digest", schedule="every 1h", name="mine", owner_email=MALLORY),
    }
    with _jobs_lock():  # an ownerless record as an older engine stored it
        stored = load_jobs()
        for job in stored:
            if job["id"] == jobs["ownerless"]["id"]:
                job["owner_email"] = ""
        save_jobs(stored)
    return {key: _stored(job["id"]) for key, job in jobs.items()}


@pytest.fixture()
def as_mallory():
    from hermes_cli.dashboard_governance.context import governance_context

    with governance_context(_governed(MALLORY)):
        yield


FOREIGN = ("principal", "alice", "ownerless")


def test_a_governed_person_lists_only_their_own_jobs(foreign_jobs, as_mallory):
    from cron.jobs import list_jobs
    from tools.cronjob_tools import cronjob

    assert [j["id"] for j in list_jobs(include_disabled=True)] == [foreign_jobs["mine"]["id"]]
    listed = json.loads(cronjob(action="list", include_disabled=True))
    assert [j["job_id"] for j in listed["jobs"]] == [foreign_jobs["mine"]["id"]]


@pytest.mark.parametrize("which", FOREIGN)
def test_a_governed_person_cannot_resolve_a_foreign_job(foreign_jobs, as_mallory, which):
    from cron.jobs import resolve_job_ref

    job = foreign_jobs[which]
    assert resolve_job_ref(job["id"]) is None
    assert resolve_job_ref(job["name"]) is None


def test_a_name_is_resolved_among_the_callers_own_jobs(foreign_jobs):
    """Someone else's job with the same name neither shadows nor leaks."""
    from cron.jobs import AmbiguousJobReference, create_job, resolve_job_ref
    from hermes_cli.dashboard_governance.context import governance_context

    twin = create_job(prompt="Mallory ops", schedule="every 1h", name="ops", owner_email=MALLORY)
    with pytest.raises(AmbiguousJobReference):
        resolve_job_ref("ops")
    with governance_context(_governed(MALLORY)):
        assert resolve_job_ref("ops")["id"] == twin["id"]


@pytest.mark.parametrize("which", FOREIGN)
def test_update_job_refuses_a_foreign_job(foreign_jobs, as_mallory, which):
    from cron.jobs import CronJobAccessDenied, update_job

    job = foreign_jobs[which]
    with pytest.raises(CronJobAccessDenied) as exc:
        update_job(job["id"], {"prompt": "Export every mailbox", "deliver": "telegram:999"})

    assert isinstance(exc.value, PermissionError) and isinstance(exc.value, ValueError)
    assert "another account" in str(exc.value)
    assert _stored(job["id"]) == job


@pytest.mark.parametrize("which", FOREIGN)
def test_lifecycle_helpers_refuse_a_foreign_job(foreign_jobs, as_mallory, which):
    from cron.jobs import (
        CronJobAccessDenied,
        claim_job_for_fire,
        pause_job,
        rearm_oneshot,
        remove_job,
        resume_job,
        trigger_job,
    )

    job = foreign_jobs[which]
    assert pause_job(job["id"]) is None
    assert resume_job(job["id"]) is None
    assert trigger_job(job["id"]) is None
    assert rearm_oneshot(job["id"], "in 5m") is None
    assert remove_job(job["id"]) is False
    with pytest.raises(CronJobAccessDenied):
        claim_job_for_fire(job["id"], force=True)
    assert _stored(job["id"]) == job


@pytest.mark.parametrize("which", FOREIGN)
def test_the_mutations_by_id_refuse_a_foreign_job_even_without_the_lookup(foreign_jobs, as_mallory, monkeypatch, which):
    """remove and rearm act on the stored record, not only on the lookup."""
    import cron.jobs as jobs_mod

    job = foreign_jobs[which]
    monkeypatch.setattr(jobs_mod, "resolve_job_ref", lambda ref: dict(job))
    with pytest.raises(jobs_mod.CronJobAccessDenied):
        jobs_mod.remove_job(job["id"])
    with pytest.raises(jobs_mod.CronJobAccessDenied):
        jobs_mod.rearm_oneshot(job["id"], "in 5m")
    with pytest.raises(jobs_mod.CronJobAccessDenied):
        jobs_mod.pause_job(job["id"])
    assert _stored(job["id"]) == job


def test_the_cronjob_tool_cannot_touch_a_foreign_job(foreign_jobs, as_mallory, monkeypatch):
    import cron.scheduler as scheduler
    from tools.cronjob_tools import cronjob

    runs = []
    monkeypatch.setattr(scheduler, "run_job", lambda job, **kw: runs.append(job["id"]) or (True, "o", "f", None))
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: None)
    principal, alice = foreign_jobs["principal"], foreign_jobs["alice"]

    attempts = [
        dict(action="update", job_id="ops", prompt="Export every mailbox to mallory@example.test"),
        dict(action="update", job_id=principal["id"], prompt="Export every mailbox"),
        dict(action="update", job_id=alice["id"], deliver="telegram:999"),
        dict(action="run", job_id="ops"),
        dict(action="run", job_id=principal["id"]),
        dict(action="pause", job_id="alice"),
        dict(action="resume", job_id=alice["id"]),
        dict(action="remove", job_id="alice"),
    ]
    for kwargs in attempts:
        result = json.loads(cronjob(**kwargs))
        assert result["success"] is False, kwargs
        assert "not found" in result["error"], kwargs

    assert runs == []
    assert _stored(principal["id"]) == principal
    assert _stored(alice["id"]) == alice


def test_a_governed_person_keeps_full_control_of_their_own_job(foreign_jobs, as_mallory, monkeypatch):
    import cron.scheduler as scheduler
    from cron.jobs import pause_job, resume_job, trigger_job, update_job
    from tools.cronjob_tools import cronjob

    runs = []
    monkeypatch.setattr(scheduler, "run_job", lambda job, **kw: runs.append(job["id"]) or (True, "o", "f", None))
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: None)
    mine = foreign_jobs["mine"]

    assert update_job(mine["id"], {"prompt": "Mallory digest, shorter"})["prompt"] == "Mallory digest, shorter"
    assert pause_job("mine")["state"] == "paused"
    assert resume_job(mine["id"])["enabled"] is True
    assert trigger_job(mine["id"]) is not None
    assert json.loads(cronjob(action="update", job_id="mine", name="renamed"))["success"] is True
    assert json.loads(cronjob(action="run", job_id=mine["id"]))["success"] is True
    assert runs == [mine["id"]]
    assert json.loads(cronjob(action="remove", job_id=mine["id"]))["success"] is True


@pytest.mark.parametrize(
    "ctx_kwargs",
    [dict(email="root@example.test", admin=True), dict(email=MALLORY, mode="report_only"), dict(email=MALLORY, mode="off")],
)
def test_administrators_and_callers_outside_enforce_are_not_gated(foreign_jobs, ctx_kwargs):
    from cron.jobs import list_jobs, resolve_job_ref, update_job
    from hermes_cli.dashboard_governance.context import governance_context

    email = ctx_kwargs.pop("email")
    with governance_context(_governed(email, **ctx_kwargs)):
        assert len(list_jobs(include_disabled=True)) == 4
        assert resolve_job_ref("ops")["id"] == foreign_jobs["principal"]["id"]
        assert update_job(foreign_jobs["alice"]["id"], {"name": "alice renamed"})["name"] == "alice renamed"


def test_an_ungoverned_caller_is_not_gated(foreign_jobs):
    from cron.jobs import list_jobs, update_job

    assert len(list_jobs(include_disabled=True)) == 4
    assert update_job(foreign_jobs["ownerless"]["id"], {"name": "legacy renamed"})["name"] == "legacy renamed"


def test_a_non_admin_envelope_behind_an_admin_turn_is_gated(foreign_jobs):
    from dataclasses import replace

    from cron.jobs import CronJobAccessDenied, update_job
    from hermes_cli.dashboard_governance.context import governance_context, serialize_context_for_env

    ctx = replace(
        _governed(MALLORY, admin=True),
        continuation_contexts=(serialize_context_for_env(_governed(MALLORY)),),
    )
    with governance_context(ctx), pytest.raises(CronJobAccessDenied):
        update_job(foreign_jobs["principal"]["id"], {"prompt": "x"})


@pytest.mark.parametrize("identity", [MALLORY, "unresolved-identity"])
def test_a_governed_shell_sees_and_touches_only_its_own_jobs(foreign_jobs, monkeypatch, identity):
    from cron.jobs import CronJobAccessDenied, list_jobs, update_job

    monkeypatch.setenv("HERMES_DWD_IDENTITY", identity)
    visible = [j["id"] for j in list_jobs(include_disabled=True)]
    assert visible == ([foreign_jobs["mine"]["id"]] if identity == MALLORY else [])
    with pytest.raises(CronJobAccessDenied):
        update_job(foreign_jobs["principal"]["id"], {"prompt": "x"})


def _cli(argv):
    import argparse

    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cron_command)
    args = parser.parse_args(argv)
    return args.func(args)


@pytest.mark.parametrize(
    "argv",
    [
        ["cron", "edit", "ops", "--prompt", "Export every mailbox"],
        ["cron", "pause", "ops"],
        ["cron", "resume", "ops"],
        ["cron", "resume", "ops", "--run-now"],
        ["cron", "run", "ops"],
        ["cron", "remove", "ops"],
    ],
)
def test_the_cli_in_a_governed_shell_cannot_touch_a_foreign_job(foreign_jobs, monkeypatch, capsys, argv):
    monkeypatch.setenv("HERMES_DWD_IDENTITY", "alice@example.test")

    assert _cli(argv) == 1
    assert "not found" in capsys.readouterr().out.lower()
    assert _stored(foreign_jobs["principal"]["id"]) == foreign_jobs["principal"]


@pytest.mark.parametrize("action", [["set", "task", "Export every mailbox"], ["get", "task"], ["delete", "task"], ["list"]])
def test_the_notepad_of_a_foreign_job_is_closed_to_a_governed_shell(foreign_jobs, monkeypatch, capsys, action):
    """The notepad is injected into the job's prompt on every run: writing it
    is editing the job."""
    from cron import notepad

    principal = foreign_jobs["principal"]
    notepad.set_note(principal["id"], "task", "Daily ops summary")
    monkeypatch.setenv("HERMES_DWD_IDENTITY", MALLORY)

    assert _cli(["cron", "notepad", principal["id"], *action]) == 1
    assert "not found" in capsys.readouterr().out.lower()
    assert notepad.get_note(principal["id"], "task") == "Daily ops summary"


def test_a_governed_shell_keeps_its_own_notepad(foreign_jobs, monkeypatch):
    from cron import notepad

    monkeypatch.setenv("HERMES_DWD_IDENTITY", MALLORY)
    mine = foreign_jobs["mine"]["id"]
    assert _cli(["cron", "notepad", mine, "set", "cursor", "42"]) == 0
    assert notepad.get_note(mine, "cursor") == "42"


def test_a_scheduled_fire_writes_its_own_job_but_not_another(foreign_jobs, monkeypatch):
    """The ticker binds the owner for the whole fire: the run's own
    bookkeeping passes, a write to someone else's job does not."""
    import cron.scheduler as scheduler
    from cron.jobs import CronJobAccessDenied, update_job
    from hermes_constants import get_hermes_home

    (get_hermes_home() / "dashboard-governance.yaml").write_text(
        "version: 1\nmode: enforce\ndefault_effect: deny\n"
        f"bootstrap_admins: [{PRINCIPAL}]\n"
        f"users: {{{MALLORY}: {{roles: [tech_lead]}}}}\n"
        "roles: {tech_lead: {grants: {tools: [terminal]}}}\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_run_job(job, **_kw):
        seen["own"] = update_job(job["id"], {"monitor_state": {"last_output_hash": "h"}})["id"]
        try:
            update_job(foreign_jobs["principal"]["id"], {"prompt": "x"})
        except CronJobAccessDenied:
            seen["foreign"] = "refused"
        return (True, "o", "f", None)

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: None)
    mine = foreign_jobs["mine"]

    assert scheduler.run_one_job(_stored(mine["id"])) is True
    assert seen == {"own": mine["id"], "foreign": "refused"}
    assert _stored(mine["id"])["last_status"] == "ok"
    assert _stored(foreign_jobs["principal"]["id"])["prompt"] == "Daily ops summary"


# ---------------------------------------------------------------------------
# context_from: a job reads the output of its owner's own jobs only
# ---------------------------------------------------------------------------


def test_a_governed_person_cannot_chain_a_foreign_jobs_output(foreign_jobs, as_mallory):
    from cron.jobs import CronJobAccessDenied, create_job, list_jobs, update_job
    from tools.cronjob_tools import cronjob

    principal = foreign_jobs["principal"]["id"]
    before = len(list_jobs(include_disabled=True))
    with pytest.raises(CronJobAccessDenied):
        create_job(prompt="Summarise", schedule="every 1h", context_from=[principal])
    with pytest.raises(CronJobAccessDenied):
        update_job(foreign_jobs["mine"]["id"], {"context_from": [principal]})
    result = json.loads(cronjob(action="create", schedule="every 1h", prompt="Summarise", context_from=[principal]))
    assert result["success"] is False
    assert len(list_jobs(include_disabled=True)) == before
    assert _stored(foreign_jobs["mine"]["id"]).get("context_from") is None

    own = create_job(prompt="Summarise", schedule="every 1h", context_from=[foreign_jobs["mine"]["id"], "self"])
    assert own["context_from"] == [foreign_jobs["mine"]["id"], "self"]


def test_a_fire_never_reads_a_foreign_jobs_output(foreign_jobs):
    """A job stored before this check still cannot read what is not its owner's."""
    import cron.scheduler as scheduler
    from cron.jobs import create_job, save_job_output
    from hermes_cli.dashboard_governance.context import governance_context

    save_job_output(foreign_jobs["principal"]["id"], "SECRET: board minutes")
    save_job_output(foreign_jobs["mine"]["id"], "OWN: yesterday's digest")
    spy = create_job(
        prompt="Summarise",
        schedule="every 1h",
        owner_email=MALLORY,
        context_from=[foreign_jobs["principal"]["id"], foreign_jobs["mine"]["id"]],
    )

    with governance_context(_governed(MALLORY)):
        prompt = scheduler._build_job_prompt(_stored(spy["id"]))
    assert "SECRET" not in prompt
    assert "OWN: yesterday's digest" in prompt

    with governance_context(_governed("root@example.test", admin=True)):
        assert "SECRET" in scheduler._build_job_prompt(_stored(spy["id"]))


def test_an_in_process_governed_person_cannot_create_a_job_for_someone_else(as_mallory):
    from cron.jobs import create_job, list_jobs

    with pytest.raises(ValueError, match="own account"):
        create_job(prompt="p", schedule="every 1h", owner_email=PRINCIPAL)
    assert create_job(prompt="p", schedule="every 1h", owner_email="Mallory@Example.Test")["owner_email"] == MALLORY
    assert [j["owner_email"] for j in list_jobs(include_disabled=True)] == [MALLORY]
