"""What a governed person may give a job: skills, a workdir, an origin.

Three inputs of a cron job were still open to every caller that
``cron.jobs._governed_caller_identities`` binds to their own jobs (a governed
non-admin under enforce, in-process or in their shell):

- **Skills.** The scheduler loads every job skill with ``skill_view`` outside
  the tool policy and injects it into every run, so a person could attach a
  skill they may not open. A new or changed skill now has to pass the
  ``skill_view`` gate under the caller's own context.
- **Workdir.** A job workdir makes the scheduler load ``AGENTS.md``,
  ``CLAUDE.md`` and ``.cursorrules`` from that directory with no file root
  check, so a person could point a job at any folder. Only an administrator
  sets one now (the WebUI Tasks panel accepts none from them at all).
- **Origin.** The cronjob tool read the origin from ``os.environ`` as a
  fallback, so a governed shell could set ``HERMES_SESSION_PLATFORM`` and
  ``HERMES_SESSION_CHAT_ID`` and aim the (immutable) origin at any chat. A
  caller bound only by its environment now takes the origin from session
  variables bound in-process, never from the environment.

Clearing a value and saving back the stored one stay allowed; administrators,
ungoverned callers and everything outside enforce are unchanged.
"""

from __future__ import annotations

import json

import pytest
import yaml

PRINCIPAL = "cron-system@example.test"
ADMIN = "root@example.test"
MALLORY = "mallory@example.test"
ALICE = "alice@example.test"
VIEWABLE = "team-digest"
SECRET = "client-secrets"


@pytest.fixture()
def policy_home(monkeypatch):
    """Mallory may open one skill; Alice may open none (no skill_view)."""
    from hermes_constants import get_hermes_home

    for name in ("HERMES_DWD_IDENTITY", "HERMES_DWD_IDENTITY_BOT_CEILING", "HERMES_DASHBOARD_GOVERNANCE_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    home = get_hermes_home()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"model": "test-model", "cron": {"system_principal": PRINCIPAL}}), encoding="utf-8"
    )
    (home / "dashboard-governance.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "enforce",
                "default_effect": "deny",
                "bootstrap_admins": [PRINCIPAL, ADMIN],
                "users": {MALLORY: {"roles": ["reader"]}, ALICE: {"roles": ["no_skills"]}},
                "roles": {
                    "reader": {"grants": {"tools": {"builtins": ["cronjob", "skill_view"]}, "skills": {"view": [VIEWABLE]}}},
                    "no_skills": {"grants": {"tools": {"builtins": ["cronjob"]}, "skills": {"view": [VIEWABLE]}}},
                },
            }
        ),
        encoding="utf-8",
    )
    return home


def _ctx(email):
    from cron.jobs import load_cron_governance_policy
    from hermes_cli.dashboard_governance.context import DashboardGovernanceContext
    from hermes_cli.dashboard_governance.models import GovernanceSubject
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access

    access = resolve_effective_access(load_cron_governance_policy(), GovernanceSubject(email=email))
    return DashboardGovernanceContext(subject=access.subject, access=access)


@pytest.fixture(params=["in-process", "shell"])
def as_governed(request, policy_home, monkeypatch):
    """Act as a governed person: their bound context, or their shell."""
    from hermes_cli.dashboard_governance.context import governance_context

    def _enter(email):
        if request.param == "shell":
            monkeypatch.setenv("HERMES_DWD_IDENTITY", email)
            return governance_context(None)
        return governance_context(_ctx(email))

    return _enter


def _stored(job_id):
    from cron.jobs import load_jobs

    return next(dict(j) for j in load_jobs() if j["id"] == job_id)


def _admin_job(owner, **kw):
    """A job an administrator set up for ``owner`` (with values they may not set)."""
    from cron.jobs import create_job

    kw.setdefault("prompt", "Weekly digest")
    kw.setdefault("schedule", "every 1h")
    kw.setdefault("name", "digest")
    return create_job(owner_email=owner, **kw)["id"]


def _cli(argv):
    import argparse

    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_command)
    args = parser.parse_args(argv)
    return args.func(args)


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skills", [[SECRET], [VIEWABLE, SECRET], [f"clients/{SECRET}"]])
def test_a_governed_person_cannot_attach_a_skill_they_may_not_open(as_governed, skills):
    from cron.jobs import CronJobAccessDenied, create_job, load_jobs

    with as_governed(MALLORY):
        with pytest.raises(CronJobAccessDenied, match="skill"):
            create_job(prompt="Summarise", schedule="every 1h", skills=skills)
    assert load_jobs() == []


@pytest.mark.parametrize("skills", [[VIEWABLE], [f"team/{VIEWABLE}"]])
def test_a_governed_person_attaches_a_skill_they_may_open(as_governed, skills):
    from cron.jobs import create_job

    with as_governed(MALLORY):
        job = create_job(prompt="Summarise", schedule="every 1h", skills=skills)
    assert job["skills"] == skills
    assert job["owner_email"] == MALLORY


def test_a_person_without_the_skill_view_tool_attaches_no_skill(as_governed):
    from cron.jobs import CronJobAccessDenied, create_job

    with as_governed(ALICE), pytest.raises(CronJobAccessDenied, match="skill"):
        create_job(prompt="Summarise", schedule="every 1h", skills=[VIEWABLE])


def test_a_job_without_skills_needs_no_skill_check(as_governed):
    from cron.jobs import create_job

    with as_governed(ALICE):
        assert create_job(prompt="Summarise", schedule="every 1h")["skills"] == []


@pytest.mark.parametrize(
    "updates",
    [
        {"skills": [SECRET]},  # the stored list, saved back
        {"skills": [SECRET], "skill": SECRET},
        {"skills": []},  # cleared
        {"skills": [SECRET, VIEWABLE]},  # a skill they may open, added
        {"skills": [VIEWABLE]},  # replaced by one they may open
        {"prompt": "Weekly digest, shorter"},  # skills untouched
    ],
)
def test_a_governed_person_keeps_clears_or_extends_the_stored_skills(as_governed, updates):
    from cron.jobs import update_job

    job_id = _admin_job(MALLORY, skills=[SECRET])
    with as_governed(MALLORY):
        updated = update_job(job_id, dict(updates))
    expected = updates.get("skills", [SECRET])
    assert updated["skills"] == expected


@pytest.mark.parametrize("updates", [{"skills": ["payroll"]}, {"skills": [SECRET, "payroll"]}, {"skill": "payroll", "skills": None}])
def test_a_governed_person_cannot_add_a_skill_they_may_not_open(as_governed, updates):
    from cron.jobs import CronJobAccessDenied, update_job

    job_id = _admin_job(MALLORY, skills=[SECRET])
    before = _stored(job_id)
    with as_governed(MALLORY), pytest.raises(CronJobAccessDenied, match="payroll"):
        update_job(job_id, dict(updates))
    assert _stored(job_id) == before


def test_the_cronjob_tool_and_the_cli_refuse_the_skill_too(policy_home, monkeypatch, capsys):
    from cron.jobs import load_jobs
    from tools.cronjob_tools import cronjob

    monkeypatch.setenv("HERMES_DWD_IDENTITY", MALLORY)
    result = json.loads(cronjob(action="create", schedule="every 1h", prompt="Summarise", skills=[SECRET]))
    assert result["success"] is False
    assert SECRET in result["error"]
    assert _cli(["cron", "create", "every 1h", "Summarise", "--skill", SECRET]) == 1
    assert SECRET in capsys.readouterr().out
    assert load_jobs() == []
    assert _cli(["cron", "create", "every 1h", "Summarise", "--skill", VIEWABLE]) == 0
    assert [job["skills"] for job in load_jobs()] == [[VIEWABLE]]


def test_a_shell_whose_policy_cannot_be_read_attaches_no_skill(policy_home, monkeypatch):
    from cron.jobs import CronJobAccessDenied, create_job, load_jobs

    monkeypatch.setenv("HERMES_DWD_IDENTITY", MALLORY)
    (policy_home / "dashboard-governance.yaml").write_text("mode: [enforce\n", encoding="utf-8")
    with pytest.raises(CronJobAccessDenied, match="could not be read"):
        create_job(prompt="Summarise", schedule="every 1h", skills=[VIEWABLE])
    assert load_jobs() == []


@pytest.mark.parametrize("who", ["admin", "ungoverned", "report_only"])
def test_administrators_and_ungoverned_callers_attach_any_skill(policy_home, monkeypatch, who):
    from cron.jobs import create_job, update_job
    from hermes_cli.dashboard_governance.context import governance_context

    ctx = None
    if who == "admin":
        ctx = _ctx(ADMIN)
    elif who == "report_only":
        policy = yaml.safe_load((policy_home / "dashboard-governance.yaml").read_text(encoding="utf-8"))
        policy["mode"] = "report_only"
        (policy_home / "dashboard-governance.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
        ctx = _ctx(MALLORY)
    with governance_context(ctx):
        job = create_job(prompt="Summarise", schedule="every 1h", skills=[SECRET], owner_email=MALLORY)
        assert update_job(job["id"], {"skills": ["payroll"]})["skills"] == ["payroll"]


# ---------------------------------------------------------------------------
# Workdir
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exists", [True, False], ids=["existing", "missing"])
def test_a_governed_person_gives_a_job_no_workdir(as_governed, tmp_path, exists):
    """The refusal is the same whether the folder exists or not."""
    from cron.jobs import CronJobAccessDenied, create_job, load_jobs

    folder = tmp_path / "clients" / "acme"
    if exists:
        folder.mkdir(parents=True)
    with as_governed(MALLORY), pytest.raises(CronJobAccessDenied, match="working directory"):
        create_job(prompt="Summarise", schedule="every 1h", workdir=str(folder))
    assert load_jobs() == []


@pytest.mark.parametrize("exists", [True, False], ids=["existing", "missing"])
def test_a_governed_person_cannot_change_the_workdir(as_governed, tmp_path, exists):
    from cron.jobs import CronJobAccessDenied, update_job

    stored_dir = tmp_path / "team"
    stored_dir.mkdir()
    job_id = _admin_job(MALLORY, workdir=str(stored_dir))
    other = tmp_path / "clients"
    if exists:
        other.mkdir()
    before = _stored(job_id)
    with as_governed(MALLORY), pytest.raises(CronJobAccessDenied, match="working directory"):
        update_job(job_id, {"workdir": str(other)})
    assert _stored(job_id) == before


@pytest.mark.parametrize("value", ["stored", "stored-with-spaces", None, "", False])
def test_a_governed_person_keeps_or_clears_the_workdir(as_governed, tmp_path, value):
    from cron.jobs import update_job

    stored_dir = tmp_path / "team"
    stored_dir.mkdir()
    job_id = _admin_job(MALLORY, workdir=str(stored_dir))
    if value == "stored":
        value, expected = str(stored_dir), str(stored_dir)
    elif value == "stored-with-spaces":
        value, expected = f"  {stored_dir}  ", str(stored_dir)
    else:
        expected = None
    with as_governed(MALLORY):
        assert update_job(job_id, {"workdir": value, "prompt": "Shorter"})["workdir"] == expected


def test_the_cronjob_tool_refuses_a_workdir_from_a_governed_shell(policy_home, monkeypatch, tmp_path):
    from cron.jobs import load_jobs
    from tools.cronjob_tools import cronjob

    monkeypatch.setenv("HERMES_DWD_IDENTITY", MALLORY)
    result = json.loads(cronjob(action="create", schedule="every 1h", prompt="Summarise", workdir=str(tmp_path)))
    assert result["success"] is False
    assert "working directory" in result["error"]
    assert load_jobs() == []


@pytest.mark.parametrize("who", ["admin", "ungoverned"])
def test_administrators_and_ungoverned_callers_set_a_workdir(policy_home, tmp_path, who):
    from cron.jobs import create_job, update_job
    from hermes_cli.dashboard_governance.context import governance_context

    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    with governance_context(_ctx(ADMIN) if who == "admin" else None):
        job = create_job(prompt="Summarise", schedule="every 1h", workdir=str(first), owner_email=MALLORY)
        assert job["workdir"] == str(first)
        assert update_job(job["id"], {"workdir": str(second)})["workdir"] == str(second)


# ---------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------


def _set_env_session(monkeypatch, platform="telegram", chat_id="-100999"):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat_id)
    monkeypatch.setenv("HERMES_SESSION_CHAT_NAME", "Board room")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "someone-else")


def test_a_governed_shell_takes_no_origin_from_its_environment(policy_home, monkeypatch, capsys):
    from cron.jobs import load_jobs
    from tools.cronjob_tools import _origin_from_env, cronjob

    monkeypatch.setenv("HERMES_DWD_IDENTITY", MALLORY)
    _set_env_session(monkeypatch)

    assert _origin_from_env() is None
    result = json.loads(cronjob(action="create", schedule="every 1h", prompt="Summarise", deliver="origin"))
    assert result["success"] is True, result
    assert _cli(["cron", "create", "every 1h", "Summarise", "--deliver", "origin", "--name", "cli"]) == 0
    jobs = load_jobs()
    assert len(jobs) == 2
    assert all(job["origin"] is None and job["owner_email"] == MALLORY for job in jobs)


def test_a_governed_shell_uses_session_variables_bound_in_process(policy_home, monkeypatch):
    from gateway.session_context import _VAR_MAP
    from tools.cronjob_tools import _origin_from_env

    monkeypatch.setenv("HERMES_DWD_IDENTITY", MALLORY)
    _set_env_session(monkeypatch)
    _VAR_MAP["HERMES_SESSION_PLATFORM"].set("telegram")
    _VAR_MAP["HERMES_SESSION_CHAT_ID"].set("12345")

    origin = _origin_from_env()
    assert origin["platform"] == "telegram"
    assert origin["chat_id"] == "12345"
    assert origin["chat_name"] is None  # never filled in from the environment
    assert origin["user_id"] is None


def test_a_webui_shaped_turn_keeps_its_origin(policy_home, monkeypatch):
    """The WebUI writes its turn's session variables in its own process and
    binds the person's context in-process: that origin is theirs."""
    from hermes_cli.dashboard_governance.context import governance_context
    from tools.cronjob_tools import _origin_from_env, cronjob

    _set_env_session(monkeypatch, platform="webui", chat_id="session-42")
    with governance_context(_ctx(MALLORY)):
        origin = _origin_from_env()
        result = json.loads(cronjob(action="create", schedule="every 1h", prompt="Summarise", deliver="origin"))
    assert origin["platform"] == "webui" and origin["chat_id"] == "session-42"
    assert result["success"] is True, result
    assert _stored(result["job_id"])["origin"]["chat_id"] == "session-42"
    assert _stored(result["job_id"])["owner_email"] == MALLORY


@pytest.mark.parametrize("who", ["ungoverned", "admin"])
def test_administrators_and_ungoverned_callers_keep_the_environment_origin(policy_home, monkeypatch, who):
    from hermes_cli.dashboard_governance.context import governance_context
    from tools.cronjob_tools import _origin_from_env

    _set_env_session(monkeypatch)
    with governance_context(_ctx(ADMIN) if who == "admin" else None):
        origin = _origin_from_env()
    assert origin["platform"] == "telegram" and origin["chat_id"] == "-100999"
    assert origin["chat_name"] == "Board room"


@pytest.mark.parametrize("fields", [{"managed_by":"synthwave"}, {"notify":{"in_app":"off"}}, {"notify":{"delivery":"off"}}])
def test_managed_attribution_and_admin_mute_need_admin(as_governed, fields):
    from cron.jobs import CronJobAccessDenied, create_job, update_job
    with as_governed(ALICE):
        job = create_job("normal task", "every 1h", notify={"in_app":"quiet"}, managed_by="client")
        assert job["owner_email"] == ALICE
        with pytest.raises(CronJobAccessDenied):
            update_job(job["id"], fields)
        with pytest.raises(CronJobAccessDenied):
            create_job("restricted task", "every 1h", **fields)
