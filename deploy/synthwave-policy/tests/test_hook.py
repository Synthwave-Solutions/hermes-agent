import json
import subprocess
import sys

import pytest

from conftest import EXAMPLE_POLICY, REPO, pe

SEND = {"command": "waapi send --to 31612345678 --text hi"}

SMALL_POLICY = """
version: 1
mode: {mode}
rules:
  - id: wa-send
    decision: ASK
    description: WhatsApp send needs approval
    tool: terminal
    command: ['^waapi\\s+send\\b']
  - id: no-prod
    decision: BLOCK
    description: Never deploy
    mcp: {{server: lovable, tool: deploy_project}}
profiles:
  marcel:
    overrides: {{wa-send: AUTO}}
  cmo:
    mode: enforce
  strict:
    defaults: {{decision: ASK}}
    disable: [no-prod]
    rules:
      - id: strict-git
        decision: BLOCK
        description: No git for this bot
        tool: terminal
        command: ['^git\\b']
"""


def write_policy(home, mode="observe", text=None):
    p = home / "policy" / "autonomy.yaml"
    p.write_text(text if text is not None else SMALL_POLICY.format(mode=mode), encoding="utf-8")
    return p


def write_root(home, mode="observe", text=None):
    """The authoritative root file (conftest points SYNTHWAVE_POLICY_ROOT at <tmp>/root)."""
    p = home.parent / "root" / "policy" / "autonomy.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if text is not None else SMALL_POLICY.format(mode=mode), encoding="utf-8")
    return p


def read_log(home):
    f = home / "policy" / "decisions.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def call(plugin, tool, args, **kw):
    return plugin._on_pre_tool_call(tool_name=tool, args=args, session_id="s1", task_id="t1",
                                    tool_call_id="c1", **kw)


# ---- observe ---------------------------------------------------------------------------------

def test_observe_never_blocks_and_logs(plugin, home):
    write_policy(home, "observe")
    assert call(plugin, "terminal", SEND) is None
    assert call(plugin, "mcp__lovable__deploy_project", {"project_id": "x"}) is None
    assert call(plugin, "terminal", {"command": "waapi status"}) is None
    log = read_log(home)
    assert [r["outcome"] for r in log] == ["would_ask", "would_block"]
    assert log[0]["rule"] == "wa-send" and log[0]["profile"] == "default"
    assert log[0]["session_id"] == "s1" and log[0]["tool_call_id"] == "c1"
    assert log[0]["args"]["command"].startswith("waapi send")


def test_observe_with_shipped_example(plugin, home):
    (home / "policy" / "autonomy.yaml").write_text(EXAMPLE_POLICY.read_text())
    assert call(plugin, "write_file", {"path": "/x/.env", "content": "SECRET_KEY=abcdef"}) is None
    log = read_log(home)
    assert log[-1]["outcome"] == "would_block" and log[-1]["rule"] == "credential-file-write"
    assert "abcdef" not in json.dumps(log), "file content must never be logged"


def test_no_policy_file_is_inert(plugin, home):
    assert call(plugin, "terminal", SEND) is None
    assert read_log(home) == []


def test_root_policy_fallback(plugin, home, tmp_path):
    root = tmp_path / "root" / "policy"
    root.mkdir(parents=True)
    (root / "autonomy.yaml").write_text(SMALL_POLICY.format(mode="enforce"))
    res = call(plugin, "terminal", SEND)
    assert res and res["action"] == "approve"
    assert (home / "policy" / "decisions.jsonl").exists(), "log goes to the profile home"


# ---- enforce ---------------------------------------------------------------------------------

def test_enforce_ask_returns_approve(plugin, home):
    write_policy(home, "enforce")
    res = call(plugin, "terminal", SEND)
    assert res["action"] == "approve"
    assert "wa-send" in res["message"]
    assert res["rule_key"].startswith("synthwave-policy:wa-send:")
    other = call(plugin, "terminal", {"command": "waapi send --to 1 --text other"})
    assert other["rule_key"] != res["rule_key"], "approval grain is one exact call"
    assert read_log(home)[-1]["outcome"] == "asked"


def test_enforce_block(plugin, home):
    write_policy(home, "enforce")
    res = call(plugin, "mcp__claude_ai_Lovable__deploy_project", {"project_id": "x"})
    assert res["action"] == "block" and "no-prod" in res["message"]
    assert read_log(home)[-1]["outcome"] == "blocked"


def test_enforce_auto_returns_none_and_does_not_log(plugin, home):
    write_policy(home, "enforce")
    assert call(plugin, "terminal", {"command": "ls -la"}) is None
    assert read_log(home) == []


def test_messages_have_no_long_dashes(plugin, home):
    write_policy(home, "enforce")
    for res in (call(plugin, "terminal", SEND), call(plugin, "mcp__lovable__deploy_project", {})):
        assert chr(0x2014) not in res["message"] and chr(0x2013) not in res["message"]


# ---- per profile -----------------------------------------------------------------------------

def test_profile_override_relaxes_rule(plugin, home):
    write_root(home, "enforce")
    assert plugin.check("terminal", SEND, home=home, profile="marcel") is None
    assert plugin.check("terminal", SEND, home=home, profile="other")["action"] == "approve"


def test_profile_mode_enforce_while_global_observe(plugin, home):
    write_root(home, "observe")
    assert plugin.check("terminal", SEND, home=home, profile="default") is None
    assert plugin.check("terminal", SEND, home=home, profile="cmo")["action"] == "approve"


def test_profile_extra_rules_disable_and_default(home):
    pol = pe.compile_policy(__import__("yaml").safe_load(SMALL_POLICY.format(mode="enforce")))
    assert pe.evaluate(pol, "terminal", {"command": "git status"}, "strict").decision == "BLOCK"
    assert pe.evaluate(pol, "mcp__lovable__deploy_project", {}, "strict").decision == "ASK"  # default ASK
    assert pe.evaluate(pol, "terminal", {"command": "git status"}, "default").decision == "AUTO"


def test_profile_detected_from_home_path(plugin, tmp_path, monkeypatch):
    root = tmp_path / "root"
    prof = root / "profiles" / "marcel"
    (prof / "policy").mkdir(parents=True)
    (root / "policy").mkdir(parents=True)
    (root / "policy" / "autonomy.yaml").write_text(SMALL_POLICY.format(mode="enforce"))
    monkeypatch.setenv("SYNTHWAVE_POLICY_ROOT", str(root))
    monkeypatch.setenv("HERMES_HOME", str(prof))
    assert pe.profile_for_home(prof) == "marcel"
    assert call(plugin, "terminal", SEND) is None  # marcel override -> AUTO


# ---- malformed policy -------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "mode: observe\nrules: [\n",                                     # YAML syntax error
    "mode: observe\nrules:\n  - id: x\n    decision: MAYBE\n    tool: terminal\n",  # bad decision
    "mode: observe\nrules:\n  - id: x\n    decision: ASK\n    command: ['([']\n",   # bad regex
    "mode: observe\nrulez: []\n",                                    # unknown key (typo)
])
def test_malformed_policy_fails_open_in_observe(plugin, home, text):
    write_policy(home, text=text)
    assert call(plugin, "terminal", SEND) is None
    log = read_log(home)
    assert log and log[-1]["decision"] == "ERROR" and log[-1]["outcome"] == "ignored"


@pytest.mark.parametrize("text", [
    "mode: enforce\nrules: [\n",
    "mode: enforce\nrules:\n  - id: x\n    decision: ASK\n    command: ['([']\n",
])
def test_malformed_policy_fails_closed_in_enforce(plugin, home, text):
    write_policy(home, text=text)
    res = call(plugin, "terminal", {"command": "ls"})
    assert res["action"] == "block"
    assert "could not be loaded" in res["message"] and "mode: observe" in res["message"]
    assert read_log(home)[-1]["outcome"] == "blocked"


def test_broken_after_good_enforce_stays_closed(plugin, home):
    p = write_policy(home, "enforce")
    assert call(plugin, "terminal", SEND)["action"] == "approve"
    p.write_text("rules: [\n  # mode line lost while editing\n")
    assert call(plugin, "terminal", {"command": "ls"})["action"] == "block"


def test_error_logged_once_per_error(plugin, home):
    write_policy(home, text="mode: observe\nrules: [\n")
    for _ in range(3):
        call(plugin, "terminal", SEND)
    assert len([r for r in read_log(home) if r["decision"] == "ERROR"]) == 1


def test_internal_error_observe_fails_open(plugin, home, monkeypatch):
    write_policy(home, "observe")
    monkeypatch.setattr(plugin, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert call(plugin, "terminal", SEND) is None


def test_internal_error_enforce_fails_closed(plugin, home, monkeypatch):
    write_policy(home, "enforce")
    plugin.check("terminal", {"command": "ls"}, home=home)  # prime last-good mode
    monkeypatch.setattr(plugin, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res = call(plugin, "terminal", SEND)
    assert res["action"] == "block" and "fails closed" in res["message"]


# ---- logging ----------------------------------------------------------------------------------

def test_rotation_keeps_one(tmp_path):
    f = tmp_path / "decisions.jsonl"
    for i in range(400):
        pe.append_jsonl(f, {"i": i, "pad": "x" * 100}, max_bytes=4096, keep=1)
    assert f.exists() and f.stat().st_size <= 4096
    assert (tmp_path / "decisions.jsonl.1").exists()
    assert not (tmp_path / "decisions.jsonl.2").exists()
    last = json.loads(f.read_text().splitlines()[-1])
    assert last["i"] == 399


def test_rotation_through_hook_uses_policy_log_settings(plugin, home):
    write_policy(home, text=SMALL_POLICY.format(mode="observe") + "log: {max_bytes: 2048, keep: 1}\n")
    for _ in range(60):
        call(plugin, "terminal", SEND)
    assert (home / "policy" / "decisions.jsonl.1").exists()
    assert (home / "policy" / "decisions.jsonl").stat().st_size <= 2048


def test_secrets_redacted_in_log(plugin, home):
    write_policy(home, "observe")
    call(plugin, "terminal", {"command": "waapi send --to 31 --text hi --token=abc123secret "
                                         "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"})
    blob = (home / "policy" / "decisions.jsonl").read_text()
    assert "abc123secret" not in blob and "eyJhbGci" not in blob


def test_policy_reload_on_change(plugin, home):
    p = write_policy(home, "observe")
    assert call(plugin, "terminal", SEND) is None
    p.write_text(SMALL_POLICY.format(mode="enforce") + "\n# changed\n")
    assert call(plugin, "terminal", SEND)["action"] == "approve"


# ---- CLIs -------------------------------------------------------------------------------------

def run_cli(*args, env=None):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=60, env=env)


def test_policy_check_cli_explains(tmp_path):
    r = run_cli(str(REPO / "bin" / "policy_check.py"), "terminal", json.dumps(SEND),
                "--policy", str(EXAMPLE_POLICY))
    assert r.returncode == 10, r.stderr
    assert "ASK" in r.stdout and "whatsapp-send" in r.stdout
    r = run_cli(str(REPO / "bin" / "policy_check.py"), "terminal", '{"command": "waapi status"}',
                "--policy", str(EXAMPLE_POLICY), "--json")
    assert r.returncode == 0 and json.loads(r.stdout)["decision"] == "AUTO"
    r = run_cli(str(REPO / "bin" / "policy_check.py"), "--list", "--policy", str(EXAMPLE_POLICY))
    assert r.returncode == 0 and "lovable-deploy-helix" in r.stdout


def test_policy_report_cli(plugin, home):
    write_policy(home, "observe")
    for _ in range(3):
        call(plugin, "terminal", SEND)
    call(plugin, "mcp__lovable__deploy_project", {})
    r = run_cli(str(REPO / "bin" / "policy_report.py"), str(home / "policy" / "decisions.jsonl"), "--json")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["total"] == 4
    assert {"rule": "wa-send", "decision": "ASK", "count": 3} in data["by_rule"]
    r = run_cli(str(REPO / "bin" / "policy_report.py"), str(home / "policy" / "decisions.jsonl"))
    assert "Per rule" in r.stdout and "wa-send" in r.stdout
