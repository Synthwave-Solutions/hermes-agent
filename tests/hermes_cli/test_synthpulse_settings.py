"""Profile configuration wins while existing managed launchers remain compatible."""
import pytest

from hermes_cli.synthpulse_settings import enabled, setting
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


@pytest.mark.parametrize("value", [False, 0, "", "configured"])
def test_explicit_configuration_wins_over_legacy_environment(monkeypatch, value):
    monkeypatch.setenv("HERMES_OPENCODE_BIN", "legacy")
    assert setting("opencode.binary", config={"opencode": {"binary": value}}) == value


@pytest.mark.parametrize("config", [{}, {"opencode": {}}, {"opencode": {"binary": None}}])
def test_missing_or_nullable_default_preserves_existing_launcher(monkeypatch, config):
    monkeypatch.setenv("HERMES_OPENCODE_BIN", "installed-cli")
    assert setting("opencode.binary", config=config) == "installed-cli"
    monkeypatch.delenv("HERMES_OPENCODE_BIN")
    assert setting("opencode.binary", "fallback", config=config) == "fallback"


@pytest.mark.parametrize("value, expected", [("false", False), ("0", False), (False, False), ("true", True)])
def test_false_values_do_not_disable_caller_governance_by_truthiness(value, expected):
    assert enabled("api_server.disable_caller_governance", False,
                   config={"api_server": {"disable_caller_governance": value}}) is expected


def test_reader_resolves_current_profile_on_every_call(monkeypatch, tmp_path):
    from hermes_cli import config
    homes = [tmp_path / "one", tmp_path / "two"]
    monkeypatch.setattr(config, "load_config_readonly", lambda: {
        "webui": {"state_dir": str(get_hermes_home() / "queue")},
    })
    for home in homes:
        token = set_hermes_home_override(home)
        try:
            assert setting("webui.state_dir") == str(home / "queue")
        finally:
            reset_hermes_home_override(token)
