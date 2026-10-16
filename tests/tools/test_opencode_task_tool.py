"""opencode_task: governance mirrors terminal, OpenCode stays confined."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context
from hermes_cli.dashboard_governance.loader import load_governance_policy
from hermes_cli.dashboard_governance.models import GovernanceSubject
from hermes_cli.dashboard_governance.resolver import resolve_effective_access
from hermes_cli.dashboard_governance.tool_policy import decide_tool_access, decide_tool_argument_access

from tools import opencode_task_runner as runner
from tools import opencode_tool

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "deploy" / "opencode" / "opencode-wrapper.sh"
FAKE_REGISTRY = SimpleNamespace(get_entry=lambda name: SimpleNamespace(toolset="terminal"),
                                get_toolset_for_tool=lambda name: "terminal")


def _access(tmp_path, user: dict, role: dict, email="dev@example.test"):
    policy = {"mode": "enforce", "roles": {"member": {"grants": role}},
              "users": {email: {"roles": ["member"], **user}}}
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy))
    subject = GovernanceSubject(email=email)
    return resolve_effective_access(load_governance_policy(path=path), subject), subject, path


def _blacklist(tmp_path, workroot: Path, **extra_user):
    role = {"permissions": ["chat:use", "terminal:use"], "profiles": ["default"],
            "tools": {"builtins": ["terminal"], "toolsets": ["terminal", "delegation"]},
            "cli": {"commands": ["*"], "workdir_roots": [str(workroot)]},
            "files": {"read_roots": [str(workroot)], "write_roots": [str(workroot)]}}
    user = {"access_level": "elevated", "access_mode": "blacklist", **extra_user}
    return _access(tmp_path, user, role)


def _whitelist_with_opencode(tmp_path, workroot: Path, **extra_user):
    role = {"permissions": ["chat:use", "terminal:use"], "profiles": ["default"],
            "tools": {"builtins": ["terminal"], "toolsets": ["terminal", "delegation"]},
            "cli": {"commands": ["opencode", "gh", "pytest"], "workdir_roots": [str(workroot)]},
            "files": {"read_roots": [str(workroot)], "write_roots": [str(workroot)]}}
    return _access(tmp_path, {"access_level": "elevated", **extra_user}, role)


def _whitelist_without_opencode(tmp_path, workroot: Path):
    role = {"permissions": ["chat:use", "terminal:use"], "profiles": ["default"],
            "tools": {"builtins": ["terminal"], "toolsets": ["terminal", "delegation"]},
            "cli": {"commands": ["gmail", "gh"], "workdir_roots": [str(workroot)]},
            "files": {"read_roots": [str(workroot)], "write_roots": [str(workroot)]}}
    return _access(tmp_path, {"access_level": "elevated"}, role)


@pytest.fixture
def workroot(tmp_path):
    root = tmp_path / "work"
    (root / "repo").mkdir(parents=True)
    return root


@pytest.mark.parametrize("maker", [_blacklist, _whitelist_with_opencode])
def test_allowed_workdir_passes_governance(tmp_path, workroot, maker):
    access, _, _ = maker(tmp_path, workroot)
    assert decide_tool_access(access, "opencode_task", FAKE_REGISTRY).allowed
    decision = decide_tool_argument_access(
        access, "opencode_task", {"task": "add a unit test", "workdir": str(workroot / "repo")})
    assert decision.allowed, decision


def test_workdir_outside_roots_is_denied(tmp_path, workroot):
    # Blacklist accounts have no workdir roots, exactly like terminal; the roots
    # bind whitelist accounts.
    access, _, _ = _whitelist_with_opencode(tmp_path, workroot)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    decision = decide_tool_argument_access(access, "opencode_task", {"task": "x", "workdir": str(outside)})
    assert not decision.allowed
    assert decision.reason in {"cli_workdir_not_allowed", "file_write_root_not_allowed", "file_root_not_allowed"}


def test_relative_workdir_is_denied(tmp_path, workroot):
    access, _, _ = _blacklist(tmp_path, workroot)
    decision = decide_tool_argument_access(access, "opencode_task", {"task": "x", "workdir": "repo"})
    assert not decision.allowed and decision.reason == "opencode_workdir_required"


def test_user_without_opencode_cli_grant_is_refused(tmp_path, workroot):
    access, _, _ = _whitelist_without_opencode(tmp_path, workroot)
    assert decide_tool_access(access, "terminal", FAKE_REGISTRY).allowed  # terminal itself is fine
    tool = decide_tool_access(access, "opencode_task", FAKE_REGISTRY)
    assert not tool.allowed and tool.reason == "opencode_cli_not_allowed"
    args = decide_tool_argument_access(access, "opencode_task", {"task": "x", "workdir": str(workroot / "repo")})
    assert not args.allowed and args.reason == "opencode_cli_not_allowed"


def test_explicit_cli_deny_blocks_blacklist_account(tmp_path, workroot):
    access, _, _ = _blacklist(tmp_path, workroot, deny={"cli": {"commands": ["opencode"]}})
    assert not decide_tool_access(access, "opencode_task", FAKE_REGISTRY).allowed


def test_task_text_naming_a_denied_path_is_refused(tmp_path, workroot):
    private = workroot / "private"
    access, _, _ = _blacklist(tmp_path, workroot, grants={"files": {"denied_globs": [str(private), str(private) + "/**"]}})
    decision = decide_tool_argument_access(
        access, "opencode_task", {"task": f"copy {private}/notes.txt into the repo", "workdir": str(workroot / "repo")})
    assert not decision.allowed
    access, _, _ = _whitelist_with_opencode(tmp_path, workroot, deny={"files": {"denied_globs": [str(private) + "/**"]}})
    decision = decide_tool_argument_access(
        access, "opencode_task", {"task": f"read {private}/notes.txt", "workdir": str(workroot / "repo")})
    assert not decision.allowed


def test_test_command_is_checked_like_terminal(tmp_path, workroot):
    access, _, _ = _whitelist_without_opencode(tmp_path, workroot)
    # whitelist account: opencode refused before the test command matters
    assert not decide_tool_argument_access(
        access, "opencode_task", {"task": "x", "workdir": str(workroot), "test_command": "rm -rf /"}).allowed
    access, _, _ = _blacklist(tmp_path, workroot, deny={"cli": {"commands": ["rm"]}})
    decision = decide_tool_argument_access(
        access, "opencode_task", {"task": "x", "workdir": str(workroot / "repo"), "test_command": "rm -rf build"})
    assert not decision.allowed


def test_owner_sees_keys_and_only_the_secret_mount_is_masked(tmp_path, workroot):
    dirs, files, patterns = opencode_tool.mask_plan(None)
    assert dirs == ["/run/synthpulse/secrets"] and files == []
    cfg = opencode_tool.opencode_config(patterns)
    perm = cfg["permission"]
    assert perm["external_directory"] == "deny"
    assert "**/.env*" not in perm["read"] and perm["read"]["*"] == "allow"
    assert perm["bash"]["*opencode*run*"] == "deny"
    assert cfg["mcp"] == {}
    assert cfg["provider"]["omniroute"]["options"]["apiKey"] == "{env:OMNIROUTE_API_KEY}"


def test_caller_denied_paths_are_masked(tmp_path, workroot):
    creds = os.path.join(str(opencode_tool._HOME), ".config/synthwave/clients")
    access, _, _ = _blacklist(tmp_path, workroot,
                              grants={"files": {"denied_globs": [creds, creds + "/**"]}})
    dirs, _, patterns = opencode_tool.mask_plan(access)
    assert creds in dirs and creds + "/**" in patterns
    cfg = opencode_tool.opencode_config(patterns)
    assert cfg["permission"]["read"][creds + "/**"] == "deny"
    assert cfg["permission"]["bash"]["*" + creds.lstrip("/") + "*"] == "deny"
    (tmp_path / "b").mkdir()
    allowed, _, _ = _blacklist(tmp_path / "b", workroot)
    dirs_ok, _, _ = opencode_tool.mask_plan(allowed)
    assert creds not in dirs_ok


def test_runner_reports_only_files_this_job_changed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    git("init", "-q")
    (repo / "a.py").write_text("a = 1\n")
    (repo / "b.py").write_text("b = 1\n")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (repo / "b.py").write_text("b = 2\n")  # dirty before the job, untouched by it
    before = runner.snapshot(repo, str(repo))
    (repo / "a.py").write_text("a = 2\n")
    (repo / "new.py").write_text("n = 1\n")
    after = runner.snapshot(repo, str(repo))
    changed = runner.changed_files(before, after)
    assert changed == ["a.py", "new.py"]
    patch = runner.collect_git({"top": str(repo)}, repo, changed)
    assert "+a = 2" in patch and "+n = 1" in patch and "b = 2" not in patch


def test_slot_limit(tmp_path):
    held = [runner.acquire_slot(tmp_path, 2, 0) for _ in range(2)]
    assert all(held)
    assert runner.acquire_slot(tmp_path, 2, 0) is None
    held[0].close()
    again = runner.acquire_slot(tmp_path, 2, 0)
    assert again is not None
    again.close()
    held[1].close()


def test_tool_refuses_inside_opencode(monkeypatch, workroot):
    monkeypatch.setenv("OPENCODE_RECURSION_GUARD", "1")
    out = json.loads(opencode_tool.opencode_task(task="x", workdir=str(workroot / "repo")))
    assert "recursion guard" in json.dumps(out)


def test_runner_refuses_inside_opencode(monkeypatch, tmp_path, capsys):
    run_dir = tmp_path / "job"
    run_dir.mkdir()
    (run_dir / "job.json").write_text(json.dumps({"id": "t", "task": "x"}))
    (run_dir / "omniroute.key").write_text("k")
    monkeypatch.setenv("OPENCODE_RECURSION_GUARD", "1")
    assert runner.main(str(run_dir)) == 2
    assert "recursion guard" in capsys.readouterr().out
    assert not (run_dir / "omniroute.key").exists()  # one-shot key is gone either way


def test_tool_blocks_governed_caller_outside_scope(tmp_path, workroot):
    access, subject, path = _whitelist_with_opencode(tmp_path, workroot)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ctx = DashboardGovernanceContext(subject=subject, access=access, active_profile="default",
                                     session_id="oc-test", approval_policy_path=str(path))
    with governance_context(ctx):
        out = json.loads(opencode_tool.opencode_task(task="x", workdir=str(outside)))
    assert "Blocked by governance" in json.dumps(out)


def test_tool_blocks_non_granted_caller(tmp_path, workroot):
    access, subject, path = _whitelist_without_opencode(tmp_path, workroot)
    ctx = DashboardGovernanceContext(subject=subject, access=access, active_profile="default",
                                     session_id="oc-test", approval_policy_path=str(path))
    with governance_context(ctx):
        out = json.loads(opencode_tool.opencode_task(task="x", workdir=str(workroot / "repo")))
    assert "opencode_cli_not_allowed" in json.dumps(out)


def _wrapper_copy(tmp_path, slots=1):
    fake = tmp_path / "fake-opencode"
    fake.write_text("#!/usr/bin/env bash\necho \"ARGS:$*\"\necho \"KEY:${OMNIROUTE_API_KEY:-}\"\n"
                    "[ -n \"${SLEEP:-}\" ] && sleep \"$SLEEP\"\nexit 0\n")
    fake.chmod(0o755)
    keyfile = tmp_path / "omniroute.key"
    keyfile.write_text("secret-key\n")
    keyfile.chmod(0o600)
    text = WRAPPER.read_text()
    text = text.replace("__OPENCODE_REAL_BIN__", str(fake)).replace("__OPENCODE_SLOT_DIR__", str(tmp_path / "slots"))
    text = text.replace("__OPENCODE_KEY_FILE__", str(keyfile)).replace('OPENCODE_MAX_SLOTS="${OPENCODE_MAX_SLOTS:-12}"', f"OPENCODE_MAX_SLOTS={slots}")
    wrapper = tmp_path / "opencode"
    wrapper.write_text(text)
    wrapper.chmod(0o755)
    return wrapper


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock missing")
def test_wrapper_guard_default_model_key_and_slots(tmp_path):
    wrapper = _wrapper_copy(tmp_path, slots=1)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    done = subprocess.run([str(wrapper), "run", "hello"], env=env, capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stderr
    assert "-m omniroute/auto/best-coding" in done.stdout
    assert "KEY:secret-key" in done.stdout
    explicit = subprocess.run([str(wrapper), "run", "-m", "omniroute/auto/coding", "hi"], env=env,
                              capture_output=True, text=True, timeout=20)
    assert "auto/best-coding" not in explicit.stdout
    nested = subprocess.run([str(wrapper), "run", "x"], env={**env, "OPENCODE_RECURSION_GUARD": "1"},
                            capture_output=True, text=True, timeout=20)
    assert nested.returncode == 1 and "recursion guard" in nested.stderr
    busy = subprocess.Popen([str(wrapper), "run", "long"], env={**env, "SLEEP": "4"},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import time
        time.sleep(1)
        second = subprocess.run([str(wrapper), "run", "x"], env={**env, "OPENCODE_SLOT_WAIT": "0"},
                                capture_output=True, text=True, timeout=20)
        assert second.returncode == 75 and "slots" in second.stderr
    finally:
        busy.wait(timeout=20)


def test_whitelist_gets_allowlist_home(tmp_path, workroot):
    access, _, _ = _whitelist_with_opencode(tmp_path, workroot)
    layout = opencode_tool.home_layout(access)
    assert layout["home_mode"] == "allowlist" and str(workroot) in layout["read_roots"]
    access, _, _ = _blacklist(tmp_path, workroot)
    assert opencode_tool.home_layout(access) == {"home_mode": "masked"}
    assert opencode_tool.home_layout(None) == {"home_mode": "masked"}


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap missing")
def test_sandbox_hides_unlisted_home_and_blocks_outside_writes(tmp_path):
    home = tmp_path / "home"
    allowed = home / "allowed"
    hidden = home / "hidden"
    for d in (allowed, hidden):
        d.mkdir(parents=True)
        (d / "f.txt").write_text("x")
    worktree = allowed / "wt"
    worktree.mkdir()
    job = {"home": str(home), "home_mode": "allowlist", "read_roots": [str(allowed)], "runtime_paths": [],
           "mask_dirs": [], "mask_files": []}
    argv = runner.bwrap_argv(job, tmp_path / "run", worktree, {"git": False})
    def sh(cmd):
        return subprocess.run(argv + ["bash", "-c", cmd], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert sh(f"cat {allowed}/f.txt").stdout == "x"
    assert sh(f"cat {hidden}/f.txt").returncode != 0
    assert sh(f"touch {allowed}/new.txt").returncode != 0
    assert sh(f"touch {worktree}/ok.txt").returncode == 0
