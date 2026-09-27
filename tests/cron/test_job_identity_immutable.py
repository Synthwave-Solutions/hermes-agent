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
