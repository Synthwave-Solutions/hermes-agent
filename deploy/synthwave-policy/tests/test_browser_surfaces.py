"""Browser and Bot Screen send surfaces."""

import json
import re
import time

import pytest

from conftest import EXAMPLE_POLICY, pe
from test_hook import SEND, SMALL_POLICY, call, read_log, write_policy, write_root

T = "terminal"


def term(cmd):
    return (T, {"command": cmd})


# ---- B5: browser and Bot Screen ------------------------------------------------------------------

def test_browser_send_surface_from_navigation(plugin, home):
    (home.parent / "root" / "policy").mkdir(parents=True)
    (home.parent / "root" / "policy" / "autonomy.yaml").write_text(
        EXAMPLE_POLICY.read_text().replace("\nmode: observe\n", "\nmode: enforce\n"))
    kw = {"session_id": "s", "task_id": "browse-1", "tool_call_id": "c"}
    assert plugin._on_pre_tool_call(tool_name="browser_navigate", args={"url": "https://example.com"}, **kw) is None
    assert plugin._on_pre_tool_call(tool_name="browser_click", args={"ref": "@e5"}, **kw) is None
    plugin._on_post_tool_call(tool_name="browser_navigate", args={"url": "https://example.com"},
                              result=json.dumps({"success": True, "url": "https://web.whatsapp.com/"}), **kw)
    res = plugin._on_pre_tool_call(tool_name="browser_press", args={"key": "Enter"}, **kw)
    assert res["action"] == "approve" and "browser-send-surface" in res["message"]
    other = dict(kw, task_id="browse-2")
    assert plugin._on_pre_tool_call(tool_name="browser_press", args={"key": "Enter"}, **other) is None
    # A human holds the bot's screen: the engine refuses anyway, the policy adds nothing.
    (home / "bot-desktop").mkdir()
    (home / "bot-desktop" / "lease.json").write_text(json.dumps({"holder": "human", "viewer_id": "v", "since": 1,
                                                                 "reason": "", "epoch": 3}))
    assert plugin._on_pre_tool_call(tool_name="browser_press", args={"key": "Enter"}, **kw) is None


@pytest.mark.parametrize("tool,args,expected", [
    ("browser_click", {"ref": "text=Verzenden"}, "ASK"),
    ("browser_click", {"ref": "button:has-text('Bestellen')"}, "ASK"),
    ("browser_exec", {"task": "click the Publish button"}, "ASK"),
    ("browser_cdp", {"method": "Runtime.evaluate", "params": {"expression": "document.querySelector('#send').click()"}}, "ASK"),
    ("browser_type", {"ref": "input[type=password]", "text": "hunter2"}, "BLOCK"),
    ("browser_cdp", {"method": "Runtime.evaluate",
                     "params": {"expression": "document.querySelector('input[type=\"password\"]').value='x'"}}, "BLOCK"),
    ("browser_click", {"ref": "@e12"}, "AUTO"),
    ("computer_use", {"action": "click", "element": 4}, "AUTO"),
])
def test_browser_label_and_password(example_policy, tool, args, expected):
    assert pe.evaluate(example_policy, tool, args, "default").decision == expected


def test_page_url_for_computer_use_and_capture(example_policy):
    url = "https://mail.google.com/mail/u/0/#inbox"
    assert pe.evaluate(example_policy, "computer_use", {"action": "click", "element": 3}, page_url=url).decision == "ASK"
    assert pe.evaluate(example_policy, "computer_use", {"action": "capture"}, page_url=url).decision == "AUTO"
    assert pe.evaluate(example_policy, "browser_click", {"ref": "@e1"}, page_url=url,
                       skip_tags=["bot-screen"]).decision == "AUTO"


def test_connector_tools_match_mcp_rules(example_policy):
    assert pe.evaluate(example_policy, "connectors__gmail__send_message", {}).decision == "ASK"


