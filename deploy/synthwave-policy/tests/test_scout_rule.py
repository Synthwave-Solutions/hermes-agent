"""Scout metadata preserves the bot's ordinary tool policy and approval gates."""

import json

import pytest

from conftest import EXAMPLE_POLICY
from test_hook import SEND, read_log, write_policy, write_root


@pytest.fixture(params=["task_id", "session_id", "environment", "collector_script"])
def scout_check(request, plugin, home, monkeypatch):
    """Compare the real hook with ordinary and persisted/marked Scout run identities."""
    (home / "cron").mkdir(exist_ok=True)
    job = {"id": "sc01", "name": "Scout", "managed_by": "scout", "owner": "michael@example.com"}
    if request.param == "collector_script":
        job.update(managed_by="client", script="scout/scout_collect.py")
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [job]}))
    identities = {
        "task_id": {"task_id": "cron:sc01:exec1"},
        "session_id": {"session_id": "cron_sc01_20261019_073000"},
        "environment": {"task_id": "cron:ordinary:exec1"},
        "collector_script": {"task_id": "cron:sc01:exec1"},
    }

    def check(tool, args, **kwargs):
        with monkeypatch.context() as env:
            env.delenv("HERMES_CRON_MANAGED_BY", raising=False)
            ordinary = plugin.check(tool, args, home=home, task_id="cron:ordinary:exec1", **kwargs)
            if request.param == "environment":
                env.setenv("HERMES_CRON_MANAGED_BY", "scout")
            scout = plugin.check(tool, args, home=home, **identities[request.param], **kwargs)
        assert scout == ordinary
        return scout

    return check


@pytest.mark.parametrize("tool,args,action", [
    ("terminal", {"command": "ls -la"}, None),
    ("terminal", {"command": "gws calendar events list"}, None),
    ("mcp__gmail__list_messages", {"query": "is:unread"}, None),
    ("connectors__calendar__list_events", {}, None),
    ("browser_navigate", {"url": "https://example.com"}, None),
    ("computer_use", {"action": "capture"}, None),
    ("delegate_task", {"task": "Summarize the research"}, None),
    ("skill_manage", {"action": "create", "name": "research-notes"}, None),
    ("execute_code", {"code": "print(1)"}, None),
    ("write_file", {"path": "/tmp/research-notes.md", "content": "Notes"}, None),
    ("terminal", SEND, "approve"),
    ("mcp__gmail__send_message", {"to": "recipient@example.com", "body": "Update"}, "approve"),
    ("send_message", {"target": "slack", "message": "Update"}, "approve"),
    ("browser_click", {"ref": "text=Verzenden"}, "approve"),
    ("browser_type", {"ref": "input[type=password]", "text": "x"}, "block"),
    ("write_file", {"path": "/tmp/.env", "content": "KEY=x"}, "block"),
])
def test_scout_uses_ordinary_tool_policy(scout_check, home, tool, args, action):
    write_root(home, text=EXAMPLE_POLICY.read_text().replace("\nmode: observe\n", "\nmode: enforce\n"))
    result = scout_check(tool, args)
    assert (result["action"] if result else None) == action


def test_scout_observe_logs_only_ordinary_policy_decisions(scout_check, home):
    write_root(home, "observe")
    assert scout_check("terminal", {"command": "ls"}) is None
    assert read_log(home) == []
    assert scout_check("terminal", SEND) is None
    assert [(row["outcome"], row["rule"]) for row in read_log(home)] == [
        ("would_ask", "wa-send"), ("would_ask", "wa-send"),
    ]


def test_scout_keeps_profile_rules_and_profile_file_tightening(scout_check, home):
    write_root(home, "enforce")
    assert scout_check("terminal", {"command": "git status"}, profile="strict")["action"] == "block"
    assert scout_check("terminal", {"command": "ls"}, profile="strict")["action"] == "approve"
    write_policy(home, text="mode: enforce\nrules:\n  - id: no-delegation\n    decision: BLOCK\n"
                            "    description: Delegation is disabled for this bot\n    tool: delegate_task\n")
    result = scout_check("delegate_task", {"task": "Summarize the research"})
    assert result["action"] == "block" and "no-delegation" in result["message"]
    assert scout_check("terminal", SEND)["action"] == "approve"


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_scout_keeps_fail_closed_policy_handling(scout_check, home, damage):
    root = write_root(home, "enforce")
    assert scout_check("terminal", {"command": "ls"}) is None
    if damage == "missing":
        root.unlink()
    else:
        root.write_text("mode: observe\nrules: [\n")
    result = scout_check("terminal", {"command": "ls"})
    assert result["action"] == "block"
    assert ("missing" if damage == "missing" else "could not be loaded") in result["message"]
