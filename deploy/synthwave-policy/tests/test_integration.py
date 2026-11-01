"""Load the plugin through the real Hermes plugin loader (temp HERMES_HOME) and fire pre_tool_call
through the engine's dispatch path. Runs in a subprocess so engine state never leaks into other tests.
Skipped when the engine checkout is not present."""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import EXAMPLE_POLICY, PLUGIN_DIR

ENGINE = Path(os.environ.get("HERMES_ENGINE_DIR", Path.home() / ".hermes" / "hermes-agent"))

pytestmark = pytest.mark.skipif(not (ENGINE / "hermes_cli" / "plugins.py").exists(),
                                reason="Hermes engine checkout not found")

DRIVER = textwrap.dedent("""
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from hermes_cli import plugins as P
    P.discover_plugins(force=True)
    info = [p for p in P.get_plugin_manager().list_plugins() if p.get("name") == "synthwave-policy"]
    out = {"plugin": info}
    calls = json.loads(sys.argv[2])
    out["results"] = []
    for tool, args in calls:
        d = P._get_pre_tool_call_directive_details(tool, args, session_id="it", tool_call_id="c")
        out["results"].append({"action": d.action, "message": d.message, "rule_key": d.rule_key})
    print("RESULT:" + json.dumps(out))
""")

CALLS = [
    ["terminal", {"command": "waapi send --to 31612345678 --text hi"}],
    ["mcp__claude_ai_Lovable__deploy_project", {"project_id": "90a96aee-8bc6-49c9-8c8c-479df5a161db"}],
    ["terminal", {"command": "waapi status"}],
]


def run_engine(tmp_path, mode, enabled=True):
    home = tmp_path / "hermes_home"
    (home / "plugins").mkdir(parents=True)
    (home / "policy").mkdir()
    shutil.copytree(PLUGIN_DIR, home / "plugins" / "synthwave-policy",
                    ignore=shutil.ignore_patterns("__pycache__"))
    policy = EXAMPLE_POLICY.read_text().replace("\nmode: observe\n", f"\nmode: {mode}\n")
    (home / "policy" / "autonomy.yaml").write_text(policy)
    enabled_list = "[synthwave-policy]" if enabled else "[]"
    (home / "config.yaml").write_text(f"plugins:\n  enabled: {enabled_list}\n")
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "SYNTHWAVE_"))}
    env.update({"HERMES_HOME": str(home), "SYNTHWAVE_POLICY_ROOT": str(home), "HOME": str(tmp_path),
                "PYTHONDONTWRITEBYTECODE": "1"})
    py = ENGINE / "venv" / "bin" / "python"
    proc = subprocess.run([str(py if py.exists() else sys.executable), str(driver), str(ENGINE),
                           json.dumps(CALLS)], cwd=str(tmp_path), env=env, capture_output=True,
                          text=True, timeout=240)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT:")), None)
    assert line, f"engine driver failed:\nSTDOUT {proc.stdout[-2000:]}\nSTDERR {proc.stderr[-4000:]}"
    return json.loads(line[len("RESULT:"):]), home


def test_engine_loads_plugin_and_enforces(tmp_path):
    out, home = run_engine(tmp_path, "enforce")
    assert out["plugin"] and out["plugin"][0]["enabled"], out
    ask, block, auto = out["results"]
    assert ask["action"] == "approve" and ask["rule_key"].startswith("synthwave-policy:whatsapp-send:")
    assert block["action"] == "block" and "lovable-deploy-helix" in block["message"]
    assert auto["action"] is None
    log = (home / "policy" / "decisions.jsonl").read_text().splitlines()
    assert [json.loads(l)["outcome"] for l in log] == ["asked", "blocked"]


def test_engine_observe_never_interferes(tmp_path):
    out, home = run_engine(tmp_path, "observe")
    assert [r["action"] for r in out["results"]] == [None, None, None]
    log = [json.loads(l) for l in (home / "policy" / "decisions.jsonl").read_text().splitlines()]
    assert [r["outcome"] for r in log] == ["would_ask", "would_block"]


def test_engine_plugin_not_enabled_is_inert(tmp_path):
    out, home = run_engine(tmp_path, "enforce", enabled=False)
    assert [r["action"] for r in out["results"]] == [None, None, None]
    assert not (home / "policy" / "decisions.jsonl").exists()
