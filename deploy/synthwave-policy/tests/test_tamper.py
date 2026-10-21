"""Tamper resistance, root-authoritative merge and the last good policy."""

import json
import re
import time

import pytest

from conftest import EXAMPLE_POLICY, pe
from test_hook import SEND, SMALL_POLICY, call, read_log, write_policy, write_root

T = "terminal"


def term(cmd):
    return (T, {"command": cmd})


# ---- B1: tamper resistance ----------------------------------------------------------------------

TAMPER = [
    ("redirect into profile policy", *term("echo 'mode: observe' > ~/.hermes/profiles/sales-ops/policy/autonomy.yaml")),
    ("rm root policy", *term("rm ~/.hermes/policy/autonomy.yaml")),
    ("mv root policy away", *term("mv ~/.hermes/policy/autonomy.yaml /tmp/x")),
    ("sed -i policy", *term("sed -i 's/enforce/observe/' /home/x/.hermes/policy/autonomy.yaml")),
    ("cp over policy", *term("cp /tmp/open.yaml ~/.hermes/policy/autonomy.yaml")),
    ("tee policy", *term("echo x | tee -a ~/.hermes/policy/autonomy.yaml")),
    ("rm marker", *term("rm -f ~/.hermes/profiles/a/policy/.enforced")),
    ("rm plugin dir", *term("rm -rf ~/.hermes/plugins/synthwave-policy")),
    ("mv plugin dir", *term("mv ~/.hermes/profiles/a/plugins/synthwave-policy /tmp/")),
    ("edit plugin code", "write_file", {"path": "/home/x/.hermes/plugins/synthwave-policy/__init__.py", "content": ""}),
    ("write policy tool", "write_file", {"path": "/home/x/.hermes/profiles/a/policy/autonomy.yaml", "content": "mode: observe"}),
    ("patch policy tool", "patch", {"path": "~/.hermes/policy/autonomy.yaml", "old_string": "enforce", "new_string": "observe"}),
    ("v4a patch policy", "patch", {"mode": "patch",
                                   "patch": "*** Begin Patch\n*** Update File: /h/.hermes/policy/autonomy.yaml\n@@\n-a\n+b\n*** End Patch"}),
    ("rm policy dir", *term("rm -rf /home/x/.hermes/policy")),
    ("chmod policy", *term("chmod 000 ~/.hermes/policy/autonomy.yaml")),
    ("hermes plugins disable", *term("hermes plugins disable synthwave-policy")),
    ("hermes -p plugins remove", *term("hermes --profile marcel plugins remove synthwave-policy")),
    ("hermes config set plugins", *term("hermes config set plugins.enabled []")),
    ("config.yaml plugins via write", "write_file",
     {"path": "/home/x/.hermes/config.yaml", "content": "plugins:\n  enabled: []\n"}),
    ("config.yaml plugins via patch", "patch",
     {"path": "/home/x/.hermes/profiles/a/config.yaml", "old_string": "plugins:\n  enabled: [synthwave-policy]",
      "new_string": "plugins:\n  enabled: []"}),
    ("config.yaml plugins via sed", *term("sed -i '/synthwave-policy/d;s/plugins/x/' ~/.hermes/config.yaml")),
]


@pytest.mark.parametrize("label,tool,args", TAMPER, ids=[t[0] for t in TAMPER])
def test_tamper_is_block(example_policy, label, tool, args):
    d = pe.evaluate(example_policy, tool, args, "default")
    assert d.decision == "BLOCK", (label, d.to_dict())


def test_config_write_without_plugins_is_ask_and_code_touch_is_ask(example_policy):
    d = pe.evaluate(example_policy, "patch", {"path": "/home/x/.hermes/config.yaml", "old_string": "model: a",
                                              "new_string": "model: b"})
    assert d.decision == "ASK" and d.rule_id == "hermes-config-write"
    d = pe.evaluate(example_policy, T, {"command": "python3 -c \"open('/h/.hermes/policy/autonomy.yaml','w')\""})
    assert d.decision in ("ASK", "BLOCK")
    d = pe.evaluate(example_policy, "write_file", {"path": "/repo/config.yaml", "content": "plugins: x"})
    assert d.decision == "AUTO", "only Hermes config files are guarded"


def test_v4a_patch_credential_file_blocked(example_policy):
    d = pe.evaluate(example_policy, "patch", {"mode": "patch",
                                              "patch": "*** Begin Patch\n*** Add File: /repo/.env\n+K=1\n*** End Patch"})
    assert d.decision == "BLOCK" and d.rule_id == "credential-file-write"


OPEN_POLICY = """
version: 1
mode: observe
rules:
  - id: wa-send
    decision: AUTO
    description: profile tries to relax
    tool: terminal
    command: ['^waapi\\\\s+send\\\\b']
profiles:
  default: {mode: observe, overrides: {wa-send: AUTO}}
"""


def test_profile_file_cannot_relax_root(plugin, home):
    write_root(home, "enforce")
    write_policy(home, text=OPEN_POLICY)  # the profile's own file says observe + AUTO
    res = call(plugin, T, SEND)
    assert res and res["action"] == "approve", "root enforce + ASK wins over the profile file"


def test_profile_file_profiles_key_is_ignored(plugin, home):
    write_root(home, "observe", text=SMALL_POLICY.format(mode="observe"))
    write_policy(home, text="mode: observe\nprofiles:\n  default: {mode: enforce}\n")
    assert call(plugin, T, SEND) is None, "per-profile settings only come from the root file"


def test_profile_file_can_tighten(plugin, home):
    write_root(home, "observe")
    write_policy(home, text="mode: enforce\nrules:\n  - id: no-git\n    decision: BLOCK\n"
                            "    description: no git here\n    tool: terminal\n    command: ['^git\\b']\n")
    assert call(plugin, T, {"command": "git status"})["action"] == "block"
    assert call(plugin, T, SEND)["action"] == "approve", "root ASK applies in the profile's enforce mode"
    assert call(plugin, T, {"command": "ls"}) is None


def test_missing_root_without_marker_is_inert(plugin, home):
    assert call(plugin, T, SEND) is None
    assert not (home / "policy" / ".enforced").exists()


def test_missing_root_after_enforce_fails_closed(plugin, home):
    root = write_root(home, "enforce")
    assert call(plugin, T, SEND)["action"] == "approve"
    marker = json.loads((home / "policy" / ".enforced").read_text())
    assert marker["sources"] == ["root"]
    root.unlink()
    res = call(plugin, T, {"command": "ls"})
    assert res["action"] == "block" and "missing" in res["message"]
    assert read_log(home)[-1]["outcome"] == "blocked"


def test_marker_from_profile_file_only(plugin, home):
    write_policy(home, "enforce")  # profile file enforces, no root file at all
    assert call(plugin, T, SEND)["action"] == "approve"
    assert call(plugin, T, {"command": "ls"}) is None, "no root file was ever enforcing"
    (home / "policy" / "autonomy.yaml").unlink()
    assert call(plugin, T, {"command": "ls"})["action"] == "block"


def test_broken_root_with_marker_fails_closed_even_in_observe_text(plugin, home):
    root = write_root(home, "enforce")
    call(plugin, T, SEND)
    root.write_text("mode: observe\nrules: [\n")  # broken, and the mode line now lies
    assert call(plugin, T, {"command": "ls"})["action"] == "block"


# ---- B4: last good full policy (per-profile enforce) ---------------------------------------------

def test_last_good_policy_keeps_per_profile_enforce(plugin, tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "policy").mkdir(parents=True)
    rootfile = root / "policy" / "autonomy.yaml"
    rootfile.write_text(SMALL_POLICY.format(mode="observe"))  # profile cmo: mode enforce
    monkeypatch.setenv("SYNTHWAVE_POLICY_ROOT", str(root))
    cmo = root / "profiles" / "cmo"
    other = root / "profiles" / "other"
    for h in (cmo, other):
        (h / "policy").mkdir(parents=True)
    assert plugin.check(T, SEND, home=cmo, profile="cmo")["action"] == "approve"
    assert plugin.check(T, SEND, home=other, profile="other") is None
    rootfile.write_text("rules: [\n  # broken while editing, no top-level mode line\n")
    assert plugin.check(T, {"command": "ls"}, home=cmo, profile="cmo")["action"] == "block"
    assert plugin.check(T, {"command": "ls"}, home=other, profile="other") is None


def test_cache_keeps_last_good_policy_object(tmp_path):
    cache = pe.PolicyCache()
    f = tmp_path / "p.yaml"
    f.write_text(SMALL_POLICY.format(mode="observe"))
    cache.get(f)
    f.write_text("rules: [\n")
    with pytest.raises(pe.PolicyError):
        cache.get(f)
    assert cache.last_good_mode_for(f, "cmo") == "enforce"
    assert cache.last_good_mode_for(f, "default") == "observe"


