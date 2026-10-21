"""Bot Screen source (read only)."""

import json
import os
import re
import sqlite3
import time

import pytest

from test_scout import WAKE_FALSE, env, last_line, run, write_jobs, write_output, write_profile  # noqa: F401

import scout_collect  # noqa: E402
import scout_ledger  # noqa: E402

SCOUT = "sc01"


def scout_doc(answer, prompt="Script Output:\nSCOUT-LEDGER: deadbeef=attached\n", failed=False):
    head = (f"# Cron Job: Scout{' (FAILED)' if failed else ''}\n\n**Job ID:** {SCOUT}\n"
            "**Run Time:** 2026-10-19 07:30:00\n**Schedule:** 30 7 * * *\n\n")
    if failed:
        return head + f"## Prompt\n\n{prompt}\n\n## Error\n\nRuntimeError: boom\n"
    return (head + f"**Prompt Characters:** {len(prompt)}\n## Prompt\n\n{prompt}\n\n"
            f"**Response Characters:** {len(answer)}\n## Response\n\n{answer}\n")


_n = [0]


def answer(env, text, failed=False):
    _n[0] += 1
    return write_output(env["home"], SCOUT, f"2026-10-19_07-30-{_n[0]:02d}.md", scout_doc(text, failed=failed),
                        mtime=time.time() + 1)


def shown_hashes(out):
    return re.findall(r"^- \[([0-9a-f]{8})\]", out, re.M)


def rows(env):
    con = sqlite3.connect(env["ledger"])
    try:
        return dict(con.execute("SELECT substr(hash,1,8), status FROM seen").fetchall())
    finally:
        con.close()


@pytest.fixture
def scout_env(env, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_JOB_ID", SCOUT)
    write_profile(env["home"])
    write_jobs(env["home"], [
        {"id": "j1", "name": "Daily", "last_status": "error", "last_error": "boom", "failure_streak": 1,
         "last_run_at": "2026-10-18T07:00:00+02:00"},
        {"id": "j2", "name": "Report", "last_status": "ok"},
        {"id": SCOUT, "name": "Scout", "script": "scout/scout_collect.py"},
    ])
    write_output(env["home"], "j2", "a.md", "## Response\n\nrevenue report ready")
    return env


# ---- A5: Bot Screen ----------------------------------------------------------------------------

def screen(env, lease=None, log=None, pid=None):
    sd = env["home"] / "bot-desktop"
    sd.mkdir(exist_ok=True)
    if lease is not None:
        (sd / "lease.json").write_text(lease if isinstance(lease, str) else json.dumps(lease))
    if log is not None:
        (sd / "launcher.log").write_text(log)
    if pid is not None:
        (sd / "launcher.pid").write_text(f"{pid} 0")
    return sd


def test_screen_human_holding_long(env, capsys):
    write_profile(env["home"])
    since = time.time() - 9 * 3600
    sd = screen(env, {"holder": "human", "viewer_id": "v1", "since": since, "reason": "log in bij Exact",
                      "epoch": 4})
    before = (sd / "lease.json").read_bytes()
    code, out = run(capsys)
    assert "Bot Screen: al lang door een mens overgenomen" in out and "log in bij Exact" in out
    assert (sd / "lease.json").read_bytes() == before, "the lease is never written"
    code, out = run(capsys)  # unclassified, so shown again: same id, not a new item
    assert out.count("Bot Screen") == 1


def test_screen_short_human_hold_and_plain_agent_are_quiet(env, capsys):
    write_profile(env["home"])
    screen(env, {"holder": "human", "viewer_id": "v1", "since": time.time() - 3600, "reason": "", "epoch": 1})
    assert last_line(run(capsys)[1]) == WAKE_FALSE
    screen(env, {"holder": "agent", "viewer_id": None, "since": time.time() - 30 * 3600, "reason": "", "epoch": 2})
    assert last_line(run(capsys)[1]) == WAKE_FALSE


def test_screen_agent_waiting_with_reason(env, capsys):
    write_profile(env["home"])
    screen(env, {"holder": "agent", "viewer_id": None, "since": time.time() - 3 * 3600,
                 "reason": "graag inloggen bij LinkedIn", "epoch": 2})
    assert "wacht op overname" in run(capsys)[1]


def test_screen_corrupt_lease(env, capsys):
    write_profile(env["home"])
    screen(env, "{not json")
    assert "lease onleesbaar" in run(capsys)[1]


def test_screen_launcher_failure(env, capsys):
    write_profile(env["home"])
    screen(env, log="dbus-daemon[1]: Activated service 'org.freedesktop.systemd1' failed\n"
                    "Xvnc did not become ready\n")
    assert "Bot Screen: starten mislukt" in run(capsys)[1]


def test_screen_launcher_noise_or_running_is_quiet(env, capsys):
    write_profile(env["home"])
    screen(env, log="dbus-daemon[1]: Activated service 'org.freedesktop.systemd1' failed: exited 1\n")
    assert last_line(run(capsys)[1]) == WAKE_FALSE
    screen(env, log="Xvnc did not become ready\n", pid=os.getpid())
    assert last_line(run(capsys)[1]) == WAKE_FALSE, "a running launcher is not a failed start"


