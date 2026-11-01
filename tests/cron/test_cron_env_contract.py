"""Real script children receive only server-owned per-fire metadata."""
import json
import os
from pathlib import Path
import pytest
from cron.run_context import normalize_context, script_environment

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    import cron.jobs as jobs
    with jobs.use_cron_store(h):
        yield h


def script(home):
    p = home / "scripts" / "context.py"
    p.write_text('import os,json\nprint(json.dumps({k:v for k,v in os.environ.items() if k.startswith("HERMES_CRON_")}))\n')
    return p.name


@pytest.mark.parametrize("trigger", ["schedule", "manual", "api"])
def test_real_no_agent_child_metadata_and_parent_unchanged(home, monkeypatch, trigger):
    from cron.scheduler import run_job
    from cron.jobs import create_job
    monkeypatch.setenv("HERMES_CRON_TRIGGER_ACTOR", "inherited-forgery")
    monkeypatch.setenv("HERMES_CRON_RUN_PROMPT", "stale-approval")
    prompt = 'RESUME nonce $(touch SHOULD_NOT_EXIST); "quoted"'
    job = create_job(None, "every 1h", name="contract", script=script(home), no_agent=True,
                     notify={"in_app": "quiet", "delivery": "failures"}, managed_by="synthwave")
    before = {k: v for k, v in os.environ.items() if k.startswith("HERMES_CRON_")}
    ok, doc, response, error = run_job(job, trigger=trigger, trigger_actor="person@example.test", run_prompt=prompt)
    assert ok, error
    data = json.loads(response)
    assert data["HERMES_CRON_JOB_ID"] == job["id"]
    assert data["HERMES_CRON_TRIGGER"] == trigger
    assert data["HERMES_CRON_TRIGGER_ACTOR"] == ("" if trigger == "schedule" else "person@example.test")
    assert data["HERMES_CRON_RUN_PROMPT"] == ("" if trigger == "schedule" else prompt)
    assert data["HERMES_CRON_NOTIFY_DELIVERY"] == "failures"
    assert data["HERMES_CRON_NOTIFY_IN_APP"] == "quiet"
    assert data["HERMES_CRON_MANAGED_BY"] == "synthwave"
    assert {k: v for k, v in os.environ.items() if k.startswith("HERMES_CRON_")} == before
    assert not (home / "scripts" / "SHOULD_NOT_EXIST").exists()
    assert "_cron_run_context" not in job


@pytest.mark.parametrize("key", ["PATH", "HERMES_DWD_IDENTITY", "GOOGLE_WORKSPACE_USER", "HERMES_HOME", "PYTHONPATH"])
def test_overlay_cannot_override_identity_or_execution_settings(home, key):
    from cron.scheduler_script import _run_job_script
    ok, output = _run_job_script(script(home), cron_env={key: "attacker"})
    assert not ok
    assert "only server-owned metadata" in output


@pytest.mark.parametrize("prompt", ["é" * 4097, "x\0y", "x" * 8193])
def test_prompt_byte_cap_and_nul(home, prompt):
    from cron.jobs import create_job, trigger_job, get_job
    job = create_job("task", "every 1h")
    before = get_job(job["id"])
    with pytest.raises(ValueError):
        trigger_job(job["id"], prompt)
    assert get_job(job["id"]) == before


def test_queued_context_survives_json_then_clears_after_fire(home):
    from cron.jobs import create_job, trigger_job, get_job, mark_job_run, update_job
    job = create_job(None, "every 1h", script=script(home), no_agent=True)
    queued = trigger_job(job["id"], "RESUME fixed nonce", trigger="api", trigger_actor="api_server:named")
    persisted = json.loads((home / "cron" / "jobs.json").read_text())
    assert "api_server:named" in json.dumps(persisted)
    env = script_environment(queued)
    assert env["HERMES_CRON_TRIGGER"] == "api"
    assert env["HERMES_CRON_TRIGGER_ACTOR"] == "api_server:named"
    for field in ("manual_run_actor", "manual_run_trigger", "_cron_run_context"):
        with pytest.raises(ValueError):
            update_job(job["id"], {field: "forgery"})
    assert mark_job_run(job["id"], True, delivery_outcome="suppressed_muted")
    after = get_job(job["id"])
    assert after["last_delivery_outcome"] == "suppressed_muted"
    assert script_environment(after)["HERMES_CRON_TRIGGER_ACTOR"] == ""
    assert script_environment(after)["HERMES_CRON_RUN_PROMPT"] == ""


def test_script_output_redaction_cannot_be_disabled(home, monkeypatch):
    from cron.scheduler import run_job
    from cron.jobs import create_job
    secret = "ghp_" + "X" * 36
    (home / "scripts" / "secret.py").write_text(f'print({secret!r})\nraise SystemExit(1)\n')
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "false")
    job = create_job(None, "every 1h", script="secret.py", no_agent=True)
    result = run_job(job)
    assert result[0] is False
    assert secret not in json.dumps(result)


def test_prerun_fallback_receives_same_context(home):
    from cron.scheduler_prompt import _build_job_prompt
    from cron.jobs import create_job
    job = create_job("Analyze the collected data", "every 1h", script=script(home))
    job["_cron_run_context"] = normalize_context("manual", "alice@example.test", "one-fire-context")
    prompt = _build_job_prompt(job)
    assert "alice@example.test" in prompt and "one-fire-context" in prompt


def test_notify_tool_roundtrip_and_legacy_toast(home):
    from tools.cronjob_tools import cronjob
    from cron.jobs import get_job
    created = json.loads(cronjob("create", prompt="daily task", schedule="every 1h", notify={"in_app":"quiet"}))
    assert created["success"], created
    jid = created["job_id"]
    assert get_job(jid)["toast_notifications"] is False
    updated = json.loads(cronjob("update", job_id=jid, notify={"in_app":"failures","delivery":"off"}))
    assert updated["success"], updated
    assert get_job(jid)["toast_notifications"] is True
    assert get_job(jid)["notify"]["delivery"] == "off"


def test_real_fire_persists_redacted_script_failure(home, monkeypatch):
    import cron.scheduler as scheduler
    from cron.jobs import create_job, get_job
    secret = "ghp_" + "Y" * 36
    (home / "scripts" / "failure.py").write_text(f'print({secret!r})\nraise SystemExit(2)\n')
    job = create_job(None, "every 1h", script="failure.py", no_agent=True, deliver="local")
    # Exercise the real fire body, store and real script child in this test process.
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda job: False)
    assert scheduler.run_one_job(job, trigger="manual", trigger_actor="operator@example.test")
    assert get_job(job["id"])["last_status"] == "error"
    for path in (home / "cron").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path.name


def test_cli_single_notification_edit_preserves_other_level(home):
    import argparse
    from hermes_cli.subcommands.cron import build_cron_parser
    from hermes_cli.cron import _job_api_kwargs
    from cron.jobs import create_job, update_job
    parser = argparse.ArgumentParser()
    build_cron_parser(parser.add_subparsers(dest="command"), cmd_cron=lambda args: None)
    job = create_job("daily", "every 1h", notify={"in_app": "quiet", "delivery": "failures"})
    args = parser.parse_args(["cron", "edit", job["id"], "--notify-in-app", "all"])
    changes = _job_api_kwargs(args)
    assert changes["notify"] == {"in_app": "all", "delivery": "failures"}
    assert update_job(job["id"], {"notify": changes["notify"]})["toast_notifications"] is True


def test_monitor_child_receives_the_same_metadata(home):
    from cron.monitor import _run_monitor_source
    job = {"id": "monitor", "monitor_script": script(home),
           "_cron_run_context": normalize_context("api", "api_server:named", "one-run")}
    ok, output = _run_monitor_source(job)
    assert ok, output
    data = json.loads(output)
    assert data["HERMES_CRON_TRIGGER"] == "api"
    assert data["HERMES_CRON_TRIGGER_ACTOR"] == "api_server:named"
    assert data["HERMES_CRON_RUN_PROMPT"] == "one-run"
