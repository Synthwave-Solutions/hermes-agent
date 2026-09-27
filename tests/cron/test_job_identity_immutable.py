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
