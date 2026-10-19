"""Scout jobs are read-only."""

import json
import re
import time

import pytest

from conftest import EXAMPLE_POLICY, pe
from test_hook import SEND, SMALL_POLICY, call, read_log, write_policy, write_root

T = "terminal"


def term(cmd):
    return (T, {"command": cmd})


# ---- B6: Scout jobs --------------------------------------------------------------------------------

def _scout_job(home, job_id="sc01", managed_by="scout", script=None):
    (home / "cron").mkdir(exist_ok=True)
    job = {"id": job_id, "name": "Scout", "managed_by": managed_by}
    if script:
        job["script"] = script
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [job]}))


def test_scout_run_blocked_in_enforce(plugin, home):
    write_root(home, "enforce")
    _scout_job(home)
    kw = {"task_id": "cron:sc01:exec1", "session_id": "cron_sc01_20261019_073000"}
    for tool, args in ((T, {"command": "ls"}), ("write_file", {"path": "/tmp/x", "content": "y"}),
                       ("send_message", {"target": "telegram", "message": "x"}), ("browser_navigate", {"url": "x"}),
                       ("computer_use", {"action": "capture"}), ("execute_code", {"code": "print(1)"})):
        res = plugin.check(tool, args, home=home, **kw)
        assert res and res["action"] == "block" and "scout-read-only" in res["message"], tool
    assert plugin.check("kanban_create", {"title": "x"}, home=home, **kw) is None
    assert plugin.check("web_search", {"query": "x"}, home=home, **kw) is None
    # the same tools outside the Scout job are untouched
    assert plugin.check(T, {"command": "ls"}, home=home, task_id="cron:other:1") is None


def test_scout_run_observe_only_logs(plugin, home):
    write_root(home, "observe")
    _scout_job(home, managed_by="client", script="scout/scout_collect.py")
    assert plugin.check(T, {"command": "ls"}, home=home, session_id="cron_sc01_20261019_073000") is None
    assert read_log(home)[-1]["outcome"] == "would_block" and read_log(home)[-1]["rule"] == "scout-read-only"


def test_scout_env_marker(plugin, home, monkeypatch):
    write_root(home, "enforce")
    monkeypatch.setenv("HERMES_CRON_MANAGED_BY", "scout")
    assert plugin.check(T, {"command": "ls"}, home=home)["action"] == "block"
