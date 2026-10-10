import json
from pathlib import Path
import pytest


def test_two_profile_scopes_a_b_a_never_share_trigger_input(tmp_path, monkeypatch):
    from cron.scheduler import run_job, _fire_secret_scope
    from cron.scheduler_provider import _profile_cron_scope
    from hermes_constants import get_hermes_home
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        (home / "scripts").mkdir(parents=True)
        (home / "config.yaml").write_text("terminal:\n  backend: local\n")
        (home / "scripts" / "show.py").write_text('import os,json\nprint(json.dumps({k:os.getenv(k) for k in ["HERMES_HOME","HERMES_CRON_TRIGGER_ACTOR","HERMES_CRON_RUN_PROMPT"]}))\n')
    seen = []
    for home in (homes[0], homes[1], homes[0]):
        with _profile_cron_scope(home), _fire_secret_scope():
            result = run_job({"id":"same-id", "name":home.name, "no_agent":True, "script":"show.py"}, trigger="manual", trigger_actor=home.name+"@example.test", run_prompt="resume-"+home.name)
            assert result[0], result[3]
            seen.append(json.loads(result[2]))
    assert seen[0] == seen[2]
    assert seen[0] != seen[1]
    for row, home in zip(seen, (homes[0],homes[1],homes[0])):
        assert row["HERMES_HOME"] == str(home)
        assert row["HERMES_CRON_TRIGGER_ACTOR"] == home.name+"@example.test"
        assert row["HERMES_CRON_RUN_PROMPT"] == "resume-"+home.name
    assert get_hermes_home() == tmp_path / "launch"


def test_governed_actor_comes_from_context_before_inherited_shell(monkeypatch):
    from types import SimpleNamespace
    from cron.run_context import governed_trigger_actor
    from hermes_cli.dashboard_governance.context import bind_governance_context, reset_governance_context
    monkeypatch.setenv("HERMES_DWD_IDENTITY", "shell@example.test")
    token = bind_governance_context(SimpleNamespace(access=SimpleNamespace(subject=SimpleNamespace(email="bound@example.test"))))
    try:
        assert governed_trigger_actor() == "bound@example.test"
    finally:
        reset_governance_context(token)
    assert governed_trigger_actor() == "shell@example.test"
