"""Tests for the profile scope guard wiring (freelancer sandboxing).

Covers the bridge (``tools/profile_scope_bridge.py``) plus the guard call
sites in ``tools/approval.py`` and ``tools/file_tools.py``: fail-open for
every non-scoped profile (byte-identical behavior), hard blocks for a
SCOPED profile outside its assigned project folders, and fail-closed on
guard errors for scoped homes only.

These tests exercise the real out-of-tree module at
``~/.hermes/profile_scope.py`` and the real profiles layout, so they skip
on machines without the scoping layer installed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.approval as approval_module
import tools.profile_scope_bridge as bridge
from tools.approval import (
    _check_profile_scope_guard,
    check_all_command_guards,
    check_execute_code_guard,
)
from tools.file_tools import (
    _check_profile_scope_path,
    _filter_profile_scope_search_results,
    patch_tool,
    read_file_tool,
    search_tool,
    write_file_tool,
)

HOME = os.path.expanduser("~")
PROFILE_SCOPE_PATH = os.path.join(HOME, ".hermes", "profile_scope.py")
STEVE_HOME = os.path.join(HOME, ".hermes", "profiles", "steve")
STEVEN_HOME = os.path.join(HOME, ".hermes", "profiles", "steven")
DEFAULT_HOME = os.path.join(HOME, ".hermes")
NON_SCOPED_HOME = os.path.join(HOME, ".hermes", "profiles", "financial-ops")

IN_SCOPE_DIR = os.path.join(HOME, "clients", "alta-ict")
OUT_OF_SCOPE = os.path.join(HOME, "clients", "heijmans", "x.txt")
SECRET_PATH = os.path.join(HOME, ".hermes", "secrets", "attio.env")
EVIL_SIBLING = os.path.join(HOME, "clients", "alta-ict-evil", "x.txt")

pytestmark = pytest.mark.skipif(
    not os.path.isfile(PROFILE_SCOPE_PATH),
    reason="~/.hermes/profile_scope.py not installed on this machine",
)


@pytest.fixture(autouse=True)
def _reset_bridge_cache():
    """The bridge caches the loaded module process-wide; reset per test so
    HERMES_HOME monkeypatching and load-failure simulation take effect."""
    bridge._reset_for_tests()
    yield
    bridge._reset_for_tests()


@pytest.fixture
def steve_home(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", STEVE_HOME)


@pytest.fixture
def broken_module(monkeypatch):
    """Simulate ~/.hermes/profile_scope.py failing to load (cached None)."""
    monkeypatch.setattr(bridge, "_PROFILE_SCOPE_MOD", None)


# ── Fail-open: default + non-scoped profiles stay byte-identical ────────────

class TestFailOpen:
    def test_default_home_command_guard_none_even_for_secrets(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", DEFAULT_HOME)
        assert _check_profile_scope_guard(f"cat {SECRET_PATH}") is None

    def test_default_home_path_guard_none_even_for_secrets(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", DEFAULT_HOME)
        assert _check_profile_scope_path(SECRET_PATH, "read") is None

    def test_unset_home_guards_none(self, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        assert _check_profile_scope_guard(f"cat {SECRET_PATH}") is None
        assert _check_profile_scope_path(SECRET_PATH, "read") is None

    def test_non_scoped_profile_guards_none(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", NON_SCOPED_HOME)
        assert _check_profile_scope_guard(f"cat {OUT_OF_SCOPE}") is None
        assert _check_profile_scope_path(OUT_OF_SCOPE, "write") is None

    def test_non_scoped_execute_code_not_scope_blocked(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", NON_SCOPED_HOME)
        result = check_execute_code_guard("print('hi')", "local")
        assert "profile scope" not in (result.get("message") or "")


# ── Scoped profile: blocked outside scope ───────────────────────────────────

class TestScopedBlocked:
    def test_command_guard_blocks_out_of_scope(self, steve_home):
        result = _check_profile_scope_guard(f"cat {OUT_OF_SCOPE}")
        assert result is not None
        assert result["approved"] is False
        assert "profile scope" in result["message"]

    def test_read_file_blocked(self, steve_home):
        out = json.loads(read_file_tool(OUT_OF_SCOPE))
        assert "Blocked by profile scope" in out["error"]

    def test_write_file_blocked(self, steve_home):
        out = json.loads(write_file_tool(OUT_OF_SCOPE, "nope"))
        assert "Blocked by profile scope" in out["error"]

    def test_patch_replace_blocked(self, steve_home):
        out = json.loads(patch_tool(mode="replace", path=OUT_OF_SCOPE,
                                    old_string="a", new_string="b"))
        assert "Blocked by profile scope" in out["error"]

    def test_search_tool_blocked_out_of_scope_root(self, steve_home):
        out = json.loads(search_tool(pattern="x",
                                     path=os.path.dirname(OUT_OF_SCOPE)))
        assert "Blocked by profile scope" in out["error"]

    def test_search_result_filter_drops_out_of_scope_matches(self, steve_home):
        result = SimpleNamespace(matches=[
            SimpleNamespace(path="/tmp/in-scope.txt"),
            SimpleNamespace(path=OUT_OF_SCOPE),
        ])
        omitted = _filter_profile_scope_search_results(result)
        assert omitted == 1
        assert [m.path for m in result.matches] == ["/tmp/in-scope.txt"]

    def test_execute_code_denied_entirely(self, steve_home):
        result = check_execute_code_guard("print('hi')", "local")
        assert result["approved"] is False
        assert "profile scope" in result["message"]

    def test_execute_code_denied_even_on_container_backend(self, steve_home):
        # Profile-keyed, not environment-keyed: the container skip must not
        # fire first.
        result = check_execute_code_guard("print('hi')", "modal")
        assert result["approved"] is False

    def test_scope_fires_before_container_skip(self, steve_home):
        result = check_all_command_guards(f"cat {OUT_OF_SCOPE}", "modal")
        assert result["approved"] is False
        assert "profile scope" in result["message"]


# ── Scoped profile: allowed inside assigned roots ───────────────────────────

class TestScopedAllowed:
    def test_command_guard_allows_project_root(self, steve_home):
        assert _check_profile_scope_guard(f"ls {IN_SCOPE_DIR}") is None

    def test_path_guard_allows_tmp_scratch(self, steve_home, tmp_path):
        target = str(tmp_path / "scratch.txt")
        if not target.startswith(("/tmp/", "/var/tmp/")):
            pytest.skip("tmp_path not under a scoped scratch root")
        assert _check_profile_scope_path(target, "write") is None

    def test_path_guard_allows_own_profile_home(self, steve_home):
        target = os.path.join(STEVE_HOME, "workspace", "notes.md")
        assert _check_profile_scope_path(target, "write") is None


# ── Fail-closed on module load failure (scoped homes only) ──────────────────

class TestFailClosedOnLoadFailure:
    def test_scoped_home_fails_closed(self, steve_home, broken_module):
        result = _check_profile_scope_guard("echo hi")
        assert result is not None
        assert result["approved"] is False
        assert "fail-closed" in result["message"]
        assert "fail-closed" in _check_profile_scope_path("/tmp/x", "read")
        assert check_execute_code_guard("print(1)", "local")["approved"] is False

    def test_default_home_fails_open(self, monkeypatch, broken_module):
        monkeypatch.setenv("HERMES_HOME", DEFAULT_HOME)
        assert _check_profile_scope_guard("echo hi") is None
        assert _check_profile_scope_path("/tmp/x", "read") is None

    def test_steven_sibling_fails_open_without_marker(self, monkeypatch,
                                                      broken_module):
        # Segment equality: "steven" must not substring-match "steve", and
        # without its own marker file it stays fail-open.
        assert not os.path.exists(
            os.path.join(STEVEN_HOME, bridge.SCOPED_PROFILE_MARKER))
        monkeypatch.setenv("HERMES_HOME", STEVEN_HOME)
        monkeypatch.delenv("HERMES_PROFILE_SCOPED", raising=False)
        assert bridge.is_scoped_home() is False
        assert _check_profile_scope_guard("echo hi") is None

    def test_steven_env_override_fails_closed(self, monkeypatch, broken_module):
        monkeypatch.setenv("HERMES_HOME", STEVEN_HOME)
        monkeypatch.setenv("HERMES_PROFILE_SCOPED", "1")
        assert bridge.is_scoped_home() is True
        result = _check_profile_scope_guard("echo hi")
        assert result is not None and result["approved"] is False

    def test_steve_marker_file_detected(self, steve_home, broken_module):
        # The on-disk marker (created for MEDIUM-7) keeps steve fail-closed
        # even when the module cannot load.
        assert bridge.is_scoped_home() is True

    def test_steven_not_scoped_when_module_loaded(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", STEVEN_HOME)
        assert bridge.is_scoped_home() is False


# ── Yolo ordering: scope fires before the yolo bypass ───────────────────────

class TestYoloOrdering:
    def test_yolo_cannot_bypass_scope(self, steve_home, monkeypatch):
        monkeypatch.setattr(approval_module,
                            "is_current_session_yolo_enabled", lambda: True)
        result = check_all_command_guards(f"cat {OUT_OF_SCOPE}", "local")
        assert result["approved"] is False
        assert "profile scope" in result["message"]

    def test_yolo_still_approves_in_scope_command(self, steve_home, monkeypatch):
        monkeypatch.setattr(approval_module,
                            "is_current_session_yolo_enabled", lambda: True)
        result = check_all_command_guards("rm -rf /tmp/stuff", "local")
        assert result["approved"] is True


# ── Patch Move File endpoints ───────────────────────────────────────────────

class TestPatchMoveFile:
    def test_move_blocked_on_out_of_scope_source(self, steve_home):
        patch = (
            "*** Begin Patch\n"
            f"*** Move File: {OUT_OF_SCOPE} -> /tmp/moved.txt\n"
            "*** End Patch"
        )
        out = json.loads(patch_tool(mode="patch", patch=patch))
        assert "Blocked by profile scope" in out["error"]

    def test_move_blocked_on_out_of_scope_destination(self, steve_home):
        patch = (
            "*** Begin Patch\n"
            f"*** Move File: /tmp/src.txt -> {OUT_OF_SCOPE}\n"
            "*** End Patch"
        )
        out = json.loads(patch_tool(mode="patch", patch=patch))
        assert "Blocked by profile scope" in out["error"]


# ── Deny roots + prefix confusion ───────────────────────────────────────────

class TestDenyAndPrefixConfusion:
    def test_secrets_path_denied(self, steve_home):
        err = _check_profile_scope_path(SECRET_PATH, "read")
        assert err is not None and "Blocked by profile scope" in err

    def test_secrets_command_denied(self, steve_home):
        result = _check_profile_scope_guard("cat ~/.hermes/secrets/attio.env")
        assert result is not None and result["approved"] is False

    def test_google_token_json_denied(self, steve_home):
        result = _check_profile_scope_guard("cat ~/.hermes/google_token.json")
        assert result is not None and result["approved"] is False

    def test_prefix_confusion_sibling_denied(self, steve_home):
        # alta-ict is in scope; alta-ict-evil must NOT prefix-match it.
        assert _check_profile_scope_path(
            os.path.join(IN_SCOPE_DIR, "x.txt"), "read") is None
        err = _check_profile_scope_path(EVIL_SIBLING, "read")
        assert err is not None and "Blocked by profile scope" in err


# ── $VAR tokens and ../ traversal with a tracked cwd ────────────────────────

class TestTokenResolution:
    def test_home_var_token_denied(self, steve_home):
        result = _check_profile_scope_guard("cat $HOME/clients/heijmans/x")
        assert result is not None and result["approved"] is False

    def test_home_var_token_allowed_in_scope(self, steve_home):
        assert _check_profile_scope_guard("ls $HOME/clients/alta-ict") is None

    def test_dotdot_traversal_denied_with_cwd(self, steve_home):
        result = _check_profile_scope_guard("cat ../catapult/x.txt",
                                            cwd=IN_SCOPE_DIR)
        assert result is not None and result["approved"] is False

    def test_dotdot_within_scope_allowed_with_cwd(self, steve_home):
        result = _check_profile_scope_guard(
            "cat ../README.md", cwd=os.path.join(IN_SCOPE_DIR, "sub"))
        assert result is None

    def test_non_scoped_tokens_unaffected(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", DEFAULT_HOME)
        assert _check_profile_scope_guard("cat $HOME/clients/heijmans/x") is None
        assert _check_profile_scope_guard("cat ../catapult/x.txt",
                                          cwd=IN_SCOPE_DIR) is None


# ── The out-of-tree module's own self-test ──────────────────────────────────

class TestSelfTest:
    def test_profile_scope_main_self_test_passes(self):
        proc = subprocess.run(
            [sys.executable, PROFILE_SCOPE_PATH],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ALL PASS" in proc.stdout


# ── The multiplexer: overridden HOME and a per-turn contextvar home ─────────
#
# The profile multiplexer serves every profile from ONE process whose HOME is
# its own root (~/.hermes-mux/home) and whose HERMES_HOME env var never names
# the caller's profile. The per-turn profile arrives as a contextvar override
# instead, and the profile homes are reached through a symlinked tree. Both
# facts silently disabled the guard for every multiplexed request until
# 2026-08-07: the module was looked up under the wrong HOME and the profile
# name was read from an env var that always said "the multiplexer".

MUX_ROOT = os.path.join(HOME, ".hermes-mux")
MUX_HOME = os.path.join(MUX_ROOT, "home")
MUX_STEVE_HOME = os.path.join(MUX_ROOT, "profiles", "steve")


class TestMultiplexerHome:
    def test_module_loads_when_home_is_overridden(self, monkeypatch):
        """expanduser must not decide where the scoping layer lives."""
        monkeypatch.setenv("HOME", MUX_HOME)
        bridge._reset_for_tests()
        assert bridge.load_profile_scope() is not None

    def test_profile_resolves_from_contextvar_override(self, monkeypatch):
        monkeypatch.setenv("HOME", MUX_HOME)
        monkeypatch.setenv("HERMES_HOME", MUX_ROOT)
        bridge._reset_for_tests()
        mod = bridge.load_profile_scope()
        assert mod is not None
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        token = set_hermes_home_override(STEVE_HOME)
        try:
            assert mod.resolve_profile() == "steve"
            assert mod.is_scoped("steve") is True
            assert bridge.is_scoped_home() is True
        finally:
            reset_hermes_home_override(token)

    def test_guard_blocks_through_the_multiplexer(self, monkeypatch):
        monkeypatch.setenv("HOME", MUX_HOME)
        monkeypatch.setenv("HERMES_HOME", MUX_ROOT)
        bridge._reset_for_tests()
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        token = set_hermes_home_override(STEVE_HOME)
        try:
            blocked = _check_profile_scope_guard(f"cat {SECRET_PATH}")
            assert blocked is not None and blocked["approved"] is False
            assert _check_profile_scope_guard(f"ls {IN_SCOPE_DIR}") is None
        finally:
            reset_hermes_home_override(token)

    def test_symlinked_profile_tree_resolves(self, monkeypatch):
        """The mux reaches profiles through ~/.hermes-mux/profiles/<name>."""
        if not os.path.islink(MUX_STEVE_HOME):
            pytest.skip("no symlinked mux profile tree on this machine")
        monkeypatch.setenv("HOME", MUX_HOME)
        bridge._reset_for_tests()
        mod = bridge.load_profile_scope()
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        token = set_hermes_home_override(MUX_STEVE_HOME)
        try:
            assert mod.resolve_profile() == "steve"
            assert bridge.is_scoped_home() is True
        finally:
            reset_hermes_home_override(token)

    def test_multiplexer_does_not_scope_a_parked_profile(self, monkeypatch):
        """Fail-open must survive the fix: parked profiles stay unbounded."""
        monkeypatch.setenv("HOME", MUX_HOME)
        monkeypatch.setenv("HERMES_HOME", MUX_ROOT)
        bridge._reset_for_tests()
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        token = set_hermes_home_override(NON_SCOPED_HOME)
        try:
            assert bridge.is_scoped_home() is False
            assert _check_profile_scope_guard(f"cat {OUT_OF_SCOPE}") is None
        finally:
            reset_hermes_home_override(token)


class TestGovernancePolicyPathUnderMux:
    """Het beleid moet vindbaar blijven als HOME naar de multiplexer wijst.

    Dit is dezelfde val als bij de folderguard: onder de mux staat HOME op
    ~/.hermes-mux/home en HERMES_HOME op de profielmap van de beller. Daar
    staat geen dashboard-governance.yaml, en de loader valt dan stil terug op
    mode "off". Alles gaat dan door zonder foutmelding.
    """

    def test_beleidspad_volgt_de_echte_gebruiker_niet_de_omgeving(self, monkeypatch):
        from gateway.platforms.api_server import _beleidspad

        monkeypatch.setenv("HOME", "/home/synthwavehq/.hermes-mux/home")
        monkeypatch.setenv("HERMES_HOME", "/home/synthwavehq/.hermes/profiles/steve")
        pad = _beleidspad()
        assert pad.endswith("/.hermes/dashboard-governance.yaml")
        assert ".hermes-mux" not in pad
        assert "/profiles/" not in pad

    def test_beleid_blijft_afdwingend_onder_de_mux(self, monkeypatch):
        from gateway.platforms.api_server import _beleidspad
        from hermes_cli.dashboard_governance import load_governance_policy

        monkeypatch.setenv("HOME", "/home/synthwavehq/.hermes-mux/home")
        beleid = load_governance_policy(path=_beleidspad())
        # Stil terugvallen op "off" is precies wat we niet willen zien.
        assert beleid.mode != "off"
