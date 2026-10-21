"""Ledger feedback without a terminal (SCOUT-LEDGER / SCOUT-WEB), re-show, watermark, notes wake."""

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


# ---- A1: SCOUT-LEDGER / SCOUT-WEB feedback ---------------------------------------------------

def test_ledger_line_classifies_and_stops_reshow(scout_env, capsys):
    code, out = run(capsys)
    hashes = shown_hashes(out)
    assert len(hashes) == 2 and "SCOUT-LEDGER" in out
    answer(scout_env, "Wat is er veranderd: x\nSCOUT-LEDGER: " + f"{hashes[0]}=notified, {hashes[1]}=saved")
    code, out = run(capsys)
    assert last_line(out) == WAKE_FALSE
    r = rows(scout_env)
    assert r[hashes[0]] == "notified" and r[hashes[1]] == "saved"
    assert "deadbeef" not in r, "lines in the prompt half are never applied"


def test_omitted_hash_is_reshown_then_auto_ignored(scout_env, capsys):
    code, out = run(capsys)
    a, b = shown_hashes(out)
    answer(scout_env, f"SCOUT-LEDGER: {a}=ignored")
    code, out = run(capsys)
    assert shown_hashes(out) == [b] and "opnieuw, 1x getoond" in out
    answer(scout_env, "Niets nieuws.\nSCOUT-LEDGER:")
    code, out = run(capsys)
    assert shown_hashes(out) == [b] and "2x getoond" in out
    answer(scout_env, "nog steeds niets")
    code, out = run(capsys)
    assert last_line(out) == WAKE_FALSE
    assert rows(scout_env)[b] == "ignored"


def test_silent_answer_ignores_all_shown_items(scout_env, capsys):
    code, out = run(capsys)
    hashes = shown_hashes(out)
    answer(scout_env, "[SILENT]")
    code, out = run(capsys)
    assert last_line(out) == WAKE_FALSE
    assert all(rows(scout_env)[h] == "ignored" for h in hashes)


def test_empty_answer_counts_as_silent(scout_env, capsys):
    code, out = run(capsys)
    answer(scout_env, "(No response generated)")
    code, out = run(capsys)
    assert last_line(out) == WAKE_FALSE


def test_failed_run_reshows(scout_env, capsys):
    code, out = run(capsys)
    hashes = shown_hashes(out)
    answer(scout_env, "", failed=True)
    code, out = run(capsys)
    assert shown_hashes(out) == hashes and "vorige Scout-run mislukte" in out


def test_missing_output_reshows(scout_env, capsys):
    code, out = run(capsys)
    hashes = shown_hashes(out)
    code, out = run(capsys)
    assert shown_hashes(out) == hashes


def test_gate_outputs_are_skipped(scout_env, capsys):
    code, out = run(capsys)
    hashes = shown_hashes(out)
    answer(scout_env, f"SCOUT-LEDGER: {hashes[0]}=saved,{hashes[1]}=saved")
    gate = "# Cron Job: Scout\n\n**Job ID:** sc01\n**Run Time:** x\n**Schedule:** y\n\n**Status:** silent (wakeAgent=false)\n"
    _n[0] += 1
    write_output(scout_env["home"], SCOUT, f"2026-10-19_07-30-{_n[0]:02d}.md", gate, mtime=time.time() + 2)
    code, out = run(capsys)
    assert last_line(out) == WAKE_FALSE


def test_strict_parsing():
    entries, web = scout_collect.parse_feedback(
        "text SCOUT-LEDGER: abcdef12=saved\n"
        "SCOUT-LEDGER: abcdef12=saved, ABCDEF34=Notified, zzzz1234=saved, abcdef56=deleted, 1234abcd=attached\n"
        "SCOUT-WEB: https://example.com/a?utm_source=x | Een titel | saved\n"
        "SCOUT-WEB: javascript:alert(1) | bad\n"
        "SCOUT-WEB: https://example.com/b | Tweede\n")
    assert entries == [("abcdef12", "saved"), ("abcdef34", "notified"), ("1234abcd", "attached")]
    assert web == [("https://example.com/a?utm_source=x", "Een titel", "saved"),
                   ("https://example.com/b", "Tweede", "notified")]
    many = "SCOUT-LEDGER: " + ",".join(f"{i:08x}=saved" for i in range(500))
    assert len(scout_collect.parse_feedback(many)[0]) == scout_collect.MAX_LEDGER_ENTRIES


def test_web_lines_recorded_and_listed_as_known(env, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_JOB_ID", SCOUT)
    write_profile(env["home"], topics=["AI act"])
    code, out = run(capsys)
    assert "Webonderwerpen" in out
    answer(env, "Gevonden.\nSCOUT-WEB: https://www.example.com/ai-act/?utm_source=x | AI act update | notified\n"
                "SCOUT-WEB: https://example.org/old | Oud nieuws | ignored")
    monkeypatch.setattr(scout_collect, "TOPIC_INTERVAL_HOURS", 0)
    code, out = run(capsys)
    assert "Al bekend, sla over:" in out and "https://www.example.com/ai-act/?utm_source=x" in out
    led = scout_ledger.Ledger(env["ledger"])
    h = scout_ledger.item_hash("web", "https://example.com/ai-act", "")
    assert led.known(h) == "notified"
    assert led.known(scout_ledger.item_hash("web", "https://example.org/old", "")) == "ignored"
    led.close()


def test_own_job_found_without_env(env, capsys, monkeypatch):
    monkeypatch.delenv("HERMES_CRON_JOB_ID", raising=False)
    write_profile(env["home"])
    write_jobs(env["home"], [{"id": "j1", "name": "Daily", "last_status": "error"},
                             {"id": SCOUT, "name": "Scout", "managed_by": "client",
                              "script": "scout/scout_collect.py"}])
    code, out = run(capsys)
    (h,) = shown_hashes(out)
    answer(env, f"SCOUT-LEDGER: {h}=ignored")
    code, out = run(capsys)
    assert last_line(out) == WAKE_FALSE


def test_classify_run_doc():
    assert scout_collect.classify_run_doc(scout_doc("[SILENT]"))[0] == "silent"
    assert scout_collect.classify_run_doc(scout_doc("Niets.\nNO_REPLY"))[0] == "silent"
    assert scout_collect.classify_run_doc(scout_doc("[CRON_FAILURE]\nboom"))[0] == "failed"
    assert scout_collect.classify_run_doc(scout_doc("", failed=True))[0] == "failed"
    kind, ans = scout_collect.classify_run_doc(scout_doc("Silent retry worked\nSCOUT-LEDGER: abcdef12=saved"))
    assert kind == "answer" and ans.endswith("=saved")


# ---- A2: watermark and notes wake ------------------------------------------------------------

def test_watermark_not_advanced_when_jobs_source_fails(env, capsys, monkeypatch):
    write_profile(env["home"])
    write_output(env["home"], "j1", "a.md", "## Response\n\nimportant output", mtime=time.time() - 3600)
    (env["home"] / "cron" / "jobs.json").write_text("{broken")
    code, out = run(capsys)
    assert "cron-jobs: niet gelezen" in out, "a notes-only run wakes the agent"
    code, out = run(capsys)
    assert last_line(out) == WAKE_FALSE, "notes-only wake at most once a day"
    write_jobs(env["home"], [{"id": "j1", "name": "Daily", "last_status": "ok"}])
    code, out = run(capsys)
    assert "important output" in out, "the failed source did not move the watermark"


def test_notes_wake_again_after_a_day(env, capsys):
    write_profile(env["home"])
    (env["home"] / "cron").mkdir(parents=True, exist_ok=True)
    (env["home"] / "cron" / "jobs.json").write_text("{broken")
    run(capsys)
    led = scout_ledger.Ledger(env["ledger"])
    led.set_meta("last_notes_wake", str(time.time() - 25 * 3600))
    led.close()
    code, out = run(capsys)
    assert "niet gelezen" in out


# ---- prompt contract -----------------------------------------------------------------------------

def test_prompt_has_no_terminal_instructions_and_documents_contract():
    text = (scout_collect.Path(scout_collect.__file__).parent / "prompt.md").read_text(encoding="utf-8")
    assert "python" not in text.lower() and "scout_ledger.py" not in text
    assert "SCOUT-LEDGER: " in text and "SCOUT-WEB: " in text and "[SILENT]" in text
