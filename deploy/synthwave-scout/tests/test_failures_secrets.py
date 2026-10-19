"""Failure streak keys and secret redaction."""

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


# ---- A3: failure streak ------------------------------------------------------------------------

def test_recovered_job_failing_again_is_reported_again(env, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_JOB_ID", SCOUT)
    write_profile(env["home"])
    fail1 = {"id": "j1", "name": "Daily", "last_status": "error", "last_error": "boom", "failure_streak": 1,
             "last_run_at": "2026-10-17T07:00:00+02:00"}
    write_jobs(env["home"], [fail1])
    code, out = run(capsys)
    (h1,) = shown_hashes(out)
    answer(env, f"SCOUT-LEDGER: {h1}=notified")
    write_jobs(env["home"], [dict(fail1, failure_streak=2, last_run_at="2026-10-18T07:00:00+02:00")])
    code, out = run(capsys)
    assert last_line(out) == WAKE_FALSE, "same streak is not reported twice"
    write_jobs(env["home"], [dict(fail1, last_status="ok", failure_streak=0)])
    code, out = run(capsys)
    write_jobs(env["home"], [dict(fail1, failure_streak=1, last_run_at="2026-10-19T07:00:00+02:00")])
    code, out = run(capsys)
    assert "Job faalt: Daily" in out and shown_hashes(out) != [h1]


# ---- A4: secrets -------------------------------------------------------------------------------

@pytest.mark.parametrize("secret", [
    "AKIAIOSFODNN7EXAMPLE",
    "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1",
    "password=hunter2xyz",
    "token: abcdef123456",
    "Authorization: Bearer abcdefghijklmnop123",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXBhcnQ",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "ghp_abcdefghijklmnopqrstuvwxyz0123",
])
def test_secrets_redacted(secret):
    out = scout_collect.clean(f"job failed with {secret} in the log", 400)
    core = re.split(r"[=:\s]", secret.replace("-----BEGIN RSA PRIVATE KEY-----", ""))
    longest = max(core, key=len)
    assert longest not in out and "REDACTED" in out


