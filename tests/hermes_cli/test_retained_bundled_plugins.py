"""A fork's retained observability plugin survives the upstream catalog migration."""

from pathlib import Path

from hermes_cli import left_core_migration as migration


def test_retained_observability_keeps_its_identity_and_does_not_install_a_duplicate(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.yaml"
    original = "plugins:\n  enabled: [observability/langfuse]\n"
    config.write_text(original)
    monkeypatch.setenv("HERMES_HOME", str(home))
    installs = []

    assert migration.migrate_home(home, install=lambda name: installs.append(name)) == []
    assert installs == []
    assert config.read_text() == original
    assert migration.legacy_plugin_alias("observability/langfuse") == ""

    from hermes_cli.plugins_cmd import _find_plugin_entry
    entry = _find_plugin_entry("observability/langfuse")
    assert entry is not None
    assert entry[5] == "observability/langfuse"
    assert Path(entry[4]).name == "langfuse"


def test_catalog_migration_still_works_when_a_distribution_does_not_retain_the_plugin(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  enabled: [observability/langfuse]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(migration, "_retained_bundled_plugin", lambda name: False)
    from hermes_cli import memory_provider_migration
    monkeypatch.setattr(memory_provider_migration, "catalog_source", lambda name: name)
    installs = []

    def install(name):
        installs.append(name)
        (home / "plugins" / name).mkdir(parents=True)
        return {"ok": True}

    assert migration.migrate_home(home, install=install) == ["langfuse"]
    assert installs == ["langfuse"]
    assert migration.legacy_plugin_alias("observability/langfuse") == "langfuse"
