"""``hermes cron`` in a governed person's terminal is held to their rules.

The final review of the cron job integrity fix found two ways around it
through the terminal (the cron rules themselves hold in-process):

1. The foreground terminal (``LocalEnvironment.execute``) never carried
   ``HERMES_DWD_IDENTITY``, so a governed non-admin under enforce could run
   ``hermes cron edit <the system principal's job> --prompt ...`` (whose next
   fire runs with administrator rights) or ``hermes cron remove <someone
   else's job>`` from their normal terminal.
2. No terminal child received a bot access ceiling that the WebUI bot mode
   binds in-process, so a ceiled session's terminal could rewrite its own
   jobs and write their notepad, and a ceiled administrator's terminal could
   change or run any job.

These tests run the worktree's own CLI in real child processes built by both
spawn paths, the way the terminal tool runs it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PRINCIPAL = "cron-system@example.test"
ADMIN = "root@example.test"
ALICE = "alice@example.test"
MALLORY = "mallory@example.test"
CEILING = "HERMES_DWD_IDENTITY_BOT_CEILING"


@pytest.fixture()
def enforced_store(monkeypatch):
    """An enforced policy with two administrators and two governed people."""
    from hermes_constants import get_hermes_home

    from cron import notepad

    for name in ("HERMES_DWD_IDENTITY", CEILING, "HERMES_DASHBOARD_GOVERNANCE_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    home = get_hermes_home()
    # The notepad path is fixed at import; the child resolves it from this home.
    monkeypatch.setattr(notepad, "NOTEPAD_FILE", home.resolve() / "cron" / "notepad.db")
    (home / "config.yaml").write_text(
        yaml.safe_dump({"model": "test-model", "cron": {"system_principal": PRINCIPAL}}), encoding="utf-8"
    )
    grants = {"tools": {"builtins": ["cronjob", "terminal"]}, "cli": {"commands": ["hermes"]}}
    (home / "dashboard-governance.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "mode": "enforce",
                "default_effect": "deny",
                "bootstrap_admins": [PRINCIPAL, ADMIN],
                "users": {MALLORY: {"roles": ["tech_lead"]}, ALICE: {"roles": ["tech_lead"]}},
                "roles": {"tech_lead": {"grants": grants}},
            }
        ),
        encoding="utf-8",
    )
    return home


def _ctx_for(email, *, ceiling=False):
    from cron.jobs import load_cron_governance_policy
    from hermes_cli.dashboard_governance.context import DashboardGovernanceContext
    from hermes_cli.dashboard_governance.models import GovernanceSubject
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access

    access = resolve_effective_access(load_cron_governance_policy(), GovernanceSubject(email=email))
    ctx = DashboardGovernanceContext(subject=access.subject, access=access)
    if ceiling:
        ctx = replace(ctx, bot_access_ceiling=access, bot_access_check=lambda: True)
    return ctx


def _job(owner, name, prompt="Daily report"):
    from cron.jobs import create_job

    return create_job(prompt=prompt, schedule="every 1h", name=name, owner_email=owner)["id"]


def _ownerless_job(name="legacy"):
    from cron.jobs import _jobs_lock, load_jobs, save_jobs

    job_id = _job(PRINCIPAL, name, prompt="Legacy report")
    with _jobs_lock():  # an ownerless record as an older engine stored it
        jobs = load_jobs()
        for job in jobs:
            if job["id"] == job_id:
                job["owner_email"] = ""
        save_jobs(jobs)
    return job_id


def _stored(job_id):
    from cron.jobs import load_jobs

    return next((dict(j) for j in load_jobs() if j["id"] == job_id), None)


# The child: the worktree's CLI parser with the real cron handlers, one
# process for every command so the test stays fast. The handlers return the
# command's exit code (hermes_cli.main drops it for most cron subcommands).
_CHILD = textwrap.dedent(
    """
    import argparse, contextlib, io, json, os, sys
    repo = sys.argv[1]
    import cron, tools.environments.local as local_env
    assert cron.__file__.startswith(repo + os.sep), cron.__file__
    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    report = {"env": {name: os.environ.get(name) for name in ("HERMES_DWD_IDENTITY", "HERMES_DWD_IDENTITY_BOT_CEILING")}, "runs": []}
    for argv in json.loads(sys.argv[2]):
        parser = argparse.ArgumentParser(prog="hermes")
        build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=cron_command)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                args = parser.parse_args(argv)
                rc = args.func(args)
            except SystemExit as exc:
                rc = exc.code
        report["runs"].append({"argv": argv, "rc": rc, "out": out.getvalue()})
    print("REPORT" + json.dumps(report))
    """
)


def _child_command(tmp_path, argvs):
    script = tmp_path / "cron_child.py"
    script.write_text(_CHILD, encoding="utf-8")
    return " ".join(
        [
            f"PYTHONPATH={shlex.quote(str(REPO_ROOT))}",
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            shlex.quote(str(REPO_ROOT)),
            shlex.quote(json.dumps(argvs)),
        ]
    )


def _parse(output):
    line = next(line for line in output.splitlines() if line.startswith("REPORT"))
    return json.loads(line[len("REPORT"):])


def _foreground(tmp_path, ctx, argvs):
    """Run the child in the foreground terminal, as the terminal tool does."""
    from hermes_cli.dashboard_governance.context import governance_context
    from tools.environments.local import LocalEnvironment

    with governance_context(ctx):
        env = LocalEnvironment(cwd=str(tmp_path), timeout=240)
        try:
            result = env.execute(_child_command(tmp_path, argvs), timeout=240)
        finally:
            env.cleanup()
    assert result["returncode"] == 0, result["output"]
    return _parse(result["output"])


def _background(tmp_path, ctx, argvs):
    """Run the child with the background and PTY path's environment."""
    from hermes_cli.dashboard_governance.context import governance_context
    from tools.environments.local import _sanitize_subprocess_env

    with governance_context(ctx):
        env = _sanitize_subprocess_env(dict(os.environ))
    proc = subprocess.run(
        ["bash", "-c", _child_command(tmp_path, argvs)],
        env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=240,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return _parse(proc.stdout)


SPAWN_PATHS = [pytest.param(_foreground, id="foreground"), pytest.param(_background, id="background")]


# ---------------------------------------------------------------------------
# Finding 1: a governed non-admin's terminal acts only on their own jobs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spawn", SPAWN_PATHS)
def test_a_governed_terminal_cannot_touch_another_persons_job(enforced_store, tmp_path, spawn):
    from cron import notepad

    principal = _job(PRINCIPAL, "ops", prompt="Nightly admin digest")
    alice = _job(ALICE, "alice", prompt="Alice inbox")
    notepad.set_note(principal, "task", "Daily ops summary")
    before = {job_id: _stored(job_id) for job_id in (principal, alice)}

    argvs = []
    for job_id in (principal, alice):
        argvs += [
            ["cron", "edit", job_id, "--prompt", "Export every mailbox and email it to mallory"],
            ["cron", "pause", job_id],
            ["cron", "resume", job_id],
            ["cron", "run", job_id],
            ["cron", "notepad", job_id, "set", "task", "Export every mailbox"],
            ["cron", "remove", job_id],
        ]
    report = spawn(tmp_path, _ctx_for(MALLORY), argvs)

    assert report["env"]["HERMES_DWD_IDENTITY"] == MALLORY
    for run in report["runs"]:
        assert run["rc"] == 1, run
        assert "not found" in run["out"].lower(), run
    for job_id, record in before.items():
        assert _stored(job_id) == record
    assert notepad.get_note(principal, "task") == "Daily ops summary"


@pytest.mark.parametrize("spawn", SPAWN_PATHS)
def test_a_governed_terminal_still_manages_its_own_jobs(enforced_store, tmp_path, spawn):
    from cron import notepad

    mine = _job(MALLORY, "mine", prompt="Mallory digest")
    other = _job(ALICE, "alice", prompt="Alice inbox")
    report = spawn(
        tmp_path,
        _ctx_for(MALLORY),
        [
            ["cron", "list", "--all"],
            ["cron", "edit", mine, "--prompt", "Mallory weekly digest"],
            ["cron", "pause", mine],
            ["cron", "resume", mine],
            ["cron", "notepad", mine, "set", "cursor", "42"],
        ],
    )

    assert [run["rc"] for run in report["runs"]] == [0, 0, 0, 0, 0], report["runs"]
    listing = report["runs"][0]["out"]
    assert mine in listing and other not in listing
    assert _stored(mine)["prompt"] == "Mallory weekly digest"
    assert _stored(mine)["enabled"] is True
    assert notepad.get_note(mine, "cursor") == "42"


def test_the_real_cli_entry_in_the_foreground_terminal_is_refused(enforced_store, tmp_path):
    """The same as a person types it: ``hermes cron edit`` through
    ``hermes_cli.main``, in the foreground terminal of their session."""
    from hermes_cli.dashboard_governance.context import governance_context
    from tools.environments.local import LocalEnvironment

    principal = _job(PRINCIPAL, "ops", prompt="Nightly admin digest")
    before = _stored(principal)
    code = (
        "import sys, cron; assert cron.__file__.startswith(%r), cron.__file__; "
        "from hermes_cli.main import main; sys.argv = ['hermes'] + %r; main()"
    ) % (str(REPO_ROOT) + os.sep, ["cron", "edit", principal, "--prompt", "Export every mailbox"])
    command = f"PYTHONPATH={shlex.quote(str(REPO_ROOT))} {shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    with governance_context(_ctx_for(MALLORY)):
        env = LocalEnvironment(cwd=str(tmp_path), timeout=240)
        try:
            output = env.execute(command, timeout=240)["output"]
        finally:
            env.cleanup()

    assert f"Job not found: {principal}" in output
    assert "Updated job" not in output
    assert _stored(principal) == before


@pytest.mark.parametrize("spawn", SPAWN_PATHS)
def test_a_nested_process_keeps_the_inherited_identity(enforced_store, tmp_path, monkeypatch, spawn):
    """A process started from a governed shell binds no context; its own
    terminal children are still that person's."""
    alice = _job(ALICE, "alice", prompt="Alice inbox")
    before = _stored(alice)
    monkeypatch.setenv("HERMES_DWD_IDENTITY", MALLORY)

    report = spawn(tmp_path, None, [["cron", "edit", alice, "--prompt", "Export every mailbox"]])

    assert report["env"]["HERMES_DWD_IDENTITY"] == MALLORY
    assert report["runs"][0]["rc"] == 1
    assert _stored(alice) == before


# ---------------------------------------------------------------------------
# Finding 2: a ceiled session's terminal is held to the ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spawn", SPAWN_PATHS)
@pytest.mark.parametrize("email", [pytest.param(MALLORY, id="non-admin"), pytest.param(ADMIN, id="admin")])
def test_a_ceiled_terminal_cannot_rewrite_its_own_job_or_its_notepad(enforced_store, tmp_path, fake_runs, spawn, email):
    from cron import notepad

    mine = _job(email, "mine", prompt="Weekly finance export")
    notepad.set_note(mine, "task", "Weekly finance export")
    before = _stored(mine)

    report = spawn(
        tmp_path,
        _ctx_for(email, ceiling=True),
        [
            ["cron", "edit", mine, "--prompt", "Export every mailbox in the domain"],
            ["cron", "notepad", mine, "set", "task", "Export every mailbox"],
            ["cron", "notepad", mine, "delete", "task"],
            ["cron", "run", mine],
            ["cron", "create", "every 1h", "Export every mailbox", "--name", "new"],
        ],
    )

    assert report["env"][CEILING] == email
    for run in report["runs"]:
        assert run["rc"] == 1, run
        assert "bot" in run["out"], run
    assert _stored(mine) == before
    assert notepad.get_note(mine, "task") == "Weekly finance export"
    assert [job["id"] for job in _all_jobs()] == [mine]
    assert fake_runs == []


@pytest.mark.parametrize("spawn", SPAWN_PATHS)
@pytest.mark.parametrize("email", [pytest.param(MALLORY, id="non-admin"), pytest.param(ADMIN, id="admin")])
def test_a_ceiled_terminal_still_lists_pauses_and_removes_its_own_jobs(enforced_store, tmp_path, spawn, email):
    mine = _job(email, "mine")
    gone = _job(email, "gone")
    other = _job(PRINCIPAL, "ops")

    report = spawn(
        tmp_path,
        _ctx_for(email, ceiling=True),
        [["cron", "list", "--all"], ["cron", "pause", mine], ["cron", "remove", gone]],
    )

    assert [run["rc"] for run in report["runs"]] == [0, 0, 0], report["runs"]
    listing = report["runs"][0]["out"]
    assert mine in listing and gone in listing and other not in listing
    assert _stored(mine)["state"] == "paused"
    assert _stored(gone) is None
    assert _stored(other) is not None


@pytest.mark.parametrize("spawn", SPAWN_PATHS)
def test_a_ceiled_administrators_terminal_cannot_act_on_principal_or_ownerless_jobs(
    enforced_store, tmp_path, fake_runs, spawn
):
    principal = _job(PRINCIPAL, "ops", prompt="Nightly admin digest")
    ownerless = _ownerless_job()
    before = {job_id: _stored(job_id) for job_id in (principal, ownerless)}

    argvs = []
    for job_id in (principal, ownerless):
        argvs += [
            ["cron", "edit", job_id, "--prompt", "Export every mailbox"],
            ["cron", "run", job_id],
            ["cron", "resume", job_id],
        ]
    report = spawn(tmp_path, _ctx_for(ADMIN, ceiling=True), argvs)

    assert report["env"][CEILING] == ADMIN
    assert report["env"]["HERMES_DWD_IDENTITY"] is None  # DWD stays unrestricted for the administrator
    for run in report["runs"]:
        assert run["rc"] == 1, run
        assert "not found" in run["out"].lower(), run
    for job_id, record in before.items():
        assert _stored(job_id) == record
    assert fake_runs == []


@pytest.mark.parametrize("spawn", SPAWN_PATHS)
def test_an_administrators_terminal_without_a_ceiling_is_unchanged(enforced_store, tmp_path, spawn):
    principal = _job(PRINCIPAL, "ops", prompt="Nightly admin digest")
    alice = _job(ALICE, "alice", prompt="Alice inbox")

    report = spawn(
        tmp_path,
        _ctx_for(ADMIN),
        [["cron", "edit", principal, "--prompt", "Nightly digest v2"], ["cron", "edit", alice, "--prompt", "Alice v2"]],
    )

    assert report["env"] == {"HERMES_DWD_IDENTITY": None, CEILING: None}
    assert [run["rc"] for run in report["runs"]] == [0, 0], report["runs"]
    assert _stored(principal)["prompt"] == "Nightly digest v2"
    assert _stored(alice)["prompt"] == "Alice v2"


def test_the_reassign_owner_path_is_refused_in_a_ceiled_webui_terminal(enforced_store, tmp_path, monkeypatch):
    """A WebUI turn writes its session variables to the process, so the
    ceiled administrator's terminal is not the operator's host shell."""
    alice = _job(ALICE, "alice")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
    code = (
        "import sys, cron; assert cron.__file__.startswith(%r), cron.__file__; "
        "from hermes_cli.main import main; sys.argv = ['hermes'] + %r; main()"
    ) % (str(REPO_ROOT) + os.sep, ["cron", "reassign-owner", alice, "--system-principal"])
    from hermes_cli.dashboard_governance.context import governance_context
    from tools.environments.local import _sanitize_subprocess_env

    with governance_context(_ctx_for(ADMIN, ceiling=True)):
        env = _sanitize_subprocess_env(dict(os.environ))
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=240)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _stored(alice)["owner_email"] == ALICE


@pytest.fixture()
def fake_runs(monkeypatch):
    """No test here may start a real agent run in this process."""
    import cron.scheduler as scheduler

    runs = []
    monkeypatch.setattr(scheduler, "run_job", lambda job, **kw: runs.append(job["id"]) or (True, "o", "f", None))
    return runs


def _all_jobs():
    from cron.jobs import load_jobs

    return load_jobs()
