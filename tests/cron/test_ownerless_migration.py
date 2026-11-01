"""Ownerless migration: behavior contracts."""
from __future__ import annotations

import tests.cron.test_ownerless_enforce as _support

pytest_plugins = ["tests.cron.test_ownerless_enforce"]


def test_the_migration_dry_run_changes_nothing(tmp_path):
    """Run as the operator would, in a fresh process: the report is complete
    and not one byte, mode or timestamp changes, in the store or in HOME."""
    store = _support._make_store(
        tmp_path / "store",
        [_support._raw_job("agent1"), _support._raw_job("script1", no_agent=True), _support._raw_job("owned1", owner="alice@example.test")],
    )
    _support._make_store(store / "profiles" / "worker", [_support._raw_job("agent2")])
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    before, home_before = _support._tree(store), _support._tree(fake_home)

    env = {
        "PATH": _support.os.environ.get("PATH", ""),
        "HOME": str(fake_home),
        "HERMES_HOME": str(store),
        "PYTHONPATH": str(_support.REPO_ROOT),
        "LANG": "C.UTF-8",
        "TZ": "UTC",
    }
    proc = _support.subprocess.run(
        [_support.sys.executable, str(_support.SCRIPT), "--all-profiles"],
        env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=120, check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Dry run: nothing was changed" in proc.stdout
    for job_id in ("agent1", "script1", "agent2"):
        assert job_id in proc.stdout
    assert "owned1" not in proc.stdout
    assert _support._tree(store) == before
    assert _support._tree(fake_home) == home_before


def test_the_migration_dry_run_reports_what_apply_would_do(capsys):
    from cron.jobs import create_job

    script = _support._load_script()
    _support._write_config(_support.PRINCIPAL)
    _support._write_policy("enforce")
    agent = _support._legacy_job(prompt="legacy agent", schedule="every 1h")
    job_script = create_job(prompt="", script="watchdog.sh", no_agent=True, schedule="every 1h")
    create_job(prompt="owned", schedule="every 1h", owner_email="alice@example.test")
    owners_before = _support._owners(_support._home())

    assert script.main(["--json"]) == 0
    report = _support.json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry_run" and report["ok"] is True
    (store,) = report["stores"]
    assert store["principal"] == _support.PRINCIPAL
    assert store["governance_mode"] == "enforce"
    assert store["principal_policy_entry"] == "admin"
    assert {(j["id"], j["kind"], j["action"]) for j in store["jobs"]} == {
        (agent["id"], "agent", "would_assign"),
        (job_script["id"], "script", "would_assign"),
    }
    assert _support._owners(_support._home()) == owners_before

    assert script.main(["--json", "--agent-only"]) == 0
    (store,) = _support.json.loads(capsys.readouterr().out)["stores"]
    assert [j["id"] for j in store["jobs"]] == [agent["id"]]


def test_the_migration_apply_assigns_the_principal_and_audits(capsys):
    from cron.jobs import create_job, owner_audit_file

    script = _support._load_script()
    _support._write_config(_support.PRINCIPAL)
    _support._write_policy("enforce")
    agent = _support._legacy_job(prompt="legacy agent", schedule="every 1h")
    job_script = create_job(prompt="", script="watchdog.sh", no_agent=True, schedule="every 1h")
    owned = create_job(prompt="owned", schedule="every 1h", owner_email="alice@example.test")

    assert script.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "Assigned the system principal to 2 job(s)" in out
    assert "Ownerless agent jobs left: 0" in out
    owners = _support._owners(_support._home())
    assert owners[agent["id"]] == _support.PRINCIPAL
    assert owners[job_script["id"]] == _support.PRINCIPAL
    assert owners[owned["id"]] == "alice@example.test"
    rows = [_support.json.loads(line) for line in owner_audit_file().read_text().splitlines()]
    assert {r["job_id"] for r in rows} == {agent["id"], job_script["id"]}
    assert all(r["source"] == "cron_assign_system_owner" for r in rows)
    assert all(r["previous_owner"] == "" and r["new_owner"] == _support.PRINCIPAL for r in rows)
    assert all(r["actor"].startswith("os:") for r in rows)

    # A second run finds nothing to do and writes no new audit rows.
    assert script.main(["--apply"]) == 0
    assert "Ownerless jobs: 0" in capsys.readouterr().out
    assert len(owner_audit_file().read_text().splitlines()) == 2


def test_the_migration_covers_every_profile(capsys):
    from cron.jobs import create_job

    script = _support._load_script()
    _support._write_config(_support.PRINCIPAL)
    _support._write_policy("enforce")
    root_job = _support._legacy_job(prompt="root", schedule="every 1h")
    profile = _support._make_store(_support._home() / "profiles" / "worker", [_support._raw_job("agent2")])

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert _support._owners(_support._home())[root_job["id"]] == _support.PRINCIPAL
    assert _support._owners(profile)["agent2"] == _support.PRINCIPAL
    assert (profile / "cron" / "owner-audit.jsonl").is_file()


def test_the_migration_takes_an_explicit_principal(capsys):
    from cron.jobs import create_job

    script = _support._load_script()
    _support._write_config(None)
    _support._write_policy("enforce", bootstrap_admins=("ops@example.test",))
    job = _support._legacy_job(prompt="legacy", schedule="every 1h")

    assert script.main(["--apply", "--principal", "Ops@Example.Test"]) == 0
    assert _support._owners(_support._home())[job["id"]] == "ops@example.test"
    assert "cron.system_principal is not set" in capsys.readouterr().out


def test_the_migration_rejects_an_invalid_principal_argument(capsys):
    script = _support._load_script()
    assert script.main(["--principal", "not an address"]) == 2


@_support.pytest.mark.parametrize(
    "setup, message",
    [
        (lambda: (_support._write_config(None), _support._write_policy("enforce")), "No system principal"),
        (lambda: (_support._write_config(_support.PRINCIPAL), _support._write_policy("enforce", bootstrap_admins=())), "no entry"),
        (lambda: (_support._write_config(_support.PRINCIPAL), _support._write_policy(raw_text="mode: [enforce\n")), "policy"),
    ],
)
def test_the_migration_refuses_to_apply_when_something_blocks(capsys, setup, message):
    from cron.jobs import create_job, owner_audit_file

    script = _support._load_script()
    setup()
    job = _support._legacy_job(prompt="legacy", schedule="every 1h")

    assert script.main([]) == 1, "the dry run already says apply would be refused"
    assert script.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert message in out and "Nothing was changed" in out
    assert _support._owners(_support._home())[job["id"]] == ""
    assert not owner_audit_file().exists()


def test_the_migration_warns_about_a_restricted_principal(capsys):
    from cron.jobs import create_job

    script = _support._load_script()
    _support._write_config(_support.PRINCIPAL)
    _support._write_policy("enforce", bootstrap_admins=(), users={_support.PRINCIPAL: {"roles": ["tech_lead"]}})
    _support._legacy_job(prompt="legacy", schedule="every 1h")

    assert script.main([]) == 0
    out = capsys.readouterr().out
    assert "principal policy entry: restricted" in out
    assert "not an administrator" in out


def test_the_migration_refuses_to_apply_from_a_governed_shell(monkeypatch):
    from cron.jobs import create_job

    script = _support._load_script()
    _support._write_config(_support.PRINCIPAL)
    _support._write_policy("enforce")
    job = _support._legacy_job(prompt="legacy", schedule="every 1h")
    monkeypatch.setenv("HERMES_DWD_IDENTITY", "mallory@example.test")

    assert script.main(["--apply"]) == 1
    assert _support._owners(_support._home())[job["id"]] == ""


def test_the_migration_reads_the_platform_policy_for_profile_stores(capsys):
    """Profile configs do not name the policy; the script must still see the
    platform's enforce mode and block when the principal has no entry."""
    script = _support._load_script()
    root = _support._platform(principal=_support.PRINCIPAL)
    policy = _support.yaml.safe_load((root / "dashboard-governance.yaml").read_text(encoding="utf-8"))
    policy["bootstrap_admins"] = []
    (root / "dashboard-governance.yaml").write_text(_support.yaml.safe_dump(policy), encoding="utf-8")
    profile = _support._Profile(root).home
    (profile / "cron" / "jobs.json").write_text(_support.json.dumps({"jobs": [_support._raw_job("agent2")]}), encoding="utf-8")

    assert script.main(["--json", "--hermes-home", str(profile)]) == 1
    (store,) = _support.json.loads(capsys.readouterr().out)["stores"]
    assert store["governance_mode"] == "enforce"
    assert store["policy_file"] == str(root / "dashboard-governance.yaml")
    assert store["principal"] == _support.PRINCIPAL
    assert any("no entry" in error for error in store["errors"])

    assert script.main(["--apply", "--hermes-home", str(profile)]) == 1
    assert _support._owners(profile)["agent2"] == ""


def test_the_migration_assigns_the_platform_principal_in_profile_stores(capsys):
    script = _support._load_script()
    root = _support._platform(principal=_support.PRINCIPAL)
    profile = _support._Profile(root).home
    (profile / "cron" / "jobs.json").write_text(_support.json.dumps({"jobs": [_support._raw_job("agent2")]}), encoding="utf-8")

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert _support._owners(profile)["agent2"] == _support.PRINCIPAL


def test_the_migration_blocks_a_profile_whose_named_policy_is_missing(capsys, tmp_path):
    """A profile that points at a policy file that does not exist would read
    as governance off. While the platform is not enforced the profile's own
    policy applies, so that is a hole, not a mode."""
    script = _support._load_script()
    root = _support._platform(principal=_support.PRINCIPAL, mode="report_only")
    cfg = {"model": "test-model", "dashboard": {"governance": {"policy_file": str(tmp_path / "gone.yaml")}}}
    profile = _support._Profile(root, config=cfg).home
    (profile / "cron" / "jobs.json").write_text(_support.json.dumps({"jobs": [_support._raw_job("agent2")]}), encoding="utf-8")

    assert script.main(["--hermes-home", str(profile)]) == 1
    assert "does not exist" in capsys.readouterr().out
    assert script.main(["--apply", "--hermes-home", str(profile)]) == 1
    assert _support._owners(profile)["agent2"] == ""


def test_the_migration_reads_an_enforced_platform_policy_over_a_profiles_own(capsys, tmp_path):
    """With the platform enforced, the profile's fires follow the platform
    policy whatever its config.yaml names, and so does the migration."""
    script = _support._load_script()
    root = _support._platform(principal=_support.PRINCIPAL)
    cfg = {"model": "test-model", "dashboard": {"governance": {"policy_file": str(tmp_path / "gone.yaml")}}}
    profile = _support._Profile(root, config=cfg).home
    (profile / "cron" / "jobs.json").write_text(_support.json.dumps({"jobs": [_support._raw_job("agent2")]}), encoding="utf-8")

    assert script.main(["--json", "--hermes-home", str(profile)]) == 0
    (store,) = _support.json.loads(capsys.readouterr().out)["stores"]
    assert store["policy_source"] == "platform"
    assert store["policy_file"] == str(root / "dashboard-governance.yaml")
    assert script.main(["--apply", "--hermes-home", str(profile)]) == 0
    assert _support._owners(profile)["agent2"] == _support.PRINCIPAL


def test_the_migration_never_overwrites_an_owner_set_meanwhile():
    from cron.jobs import create_job, reassign_job_owner

    script = _support._load_script()
    _support._write_config(_support.PRINCIPAL)
    _support._write_policy("enforce")
    job = _support._legacy_job(prompt="legacy", schedule="every 1h")
    report = script.inspect_store(_support._home(), principal_override="", agent_only=False)
    reassign_job_owner(job["id"], "alice@example.test", actor="os:tester")

    script.apply_store(report, actor="os:tester", reason="test")

    (item,) = report["jobs"]
    assert item["action"] == "skipped"
    assert _support._owners(_support._home())[job["id"]] == "alice@example.test"


def test_the_migration_reports_profile_stores_even_without_all_profiles(capsys):
    """The root store is clean, a profile store is not: without
    --all-profiles the script must say so and must not exit 0."""
    from cron.jobs import create_job

    script = _support._load_script()
    _support._write_config(_support.PRINCIPAL)
    _support._write_policy("enforce")
    create_job(prompt="owned", schedule="every 1h", owner_email="alice@example.test")
    profile = _support._make_store(
        _support._home() / "profiles" / "worker",
        [_support._raw_job("agent2"), _support._raw_job("script2", no_agent=True), _support._raw_job("owned2", owner="alice@example.test")],
    )

    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert str(profile) in out and "agent2" in out and "--all-profiles" in out
    assert "script2" not in out, "a script job is not refused, so it does not hold up the all clear"
    assert "All clear" not in out

    assert script.main(["--json"]) == 1
    report = _support.json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert [s_["home"] for s_ in report["stores"]] == [str(_support._home().resolve())]
    (uncovered,) = report["uncovered_stores"]
    assert uncovered["home"] == str(profile.resolve())
    assert uncovered["ownerless_agent_jobs"] == ["agent2"]

    assert script.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "Not all clear" in out and "All clear" not in out
    assert _support._owners(profile)["agent2"] == "", "a store that was not selected is not changed"

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert "All clear" in capsys.readouterr().out
    assert _support._owners(profile)["agent2"] == _support.PRINCIPAL


def test_the_migration_checks_the_whole_platform_when_given_one_profile(capsys):
    from cron.jobs import create_job

    script = _support._load_script()
    _support._write_config(_support.PRINCIPAL)
    _support._write_policy("enforce")
    left = _support._legacy_job(prompt="root legacy", schedule="every 1h")
    profile = _support._make_store(_support._home() / "profiles" / "worker", [_support._raw_job("agent2")])

    assert script.main(["--apply", "--hermes-home", str(profile)]) == 1
    out = capsys.readouterr().out
    assert _support._owners(profile)["agent2"] == _support.PRINCIPAL
    assert left["id"] in out and "Not all clear" in out


def test_the_migration_gives_a_personal_profile_job_to_its_person(capsys):
    """The administrator principal in a governed person's own profile would
    run their jobs with administrator rights. The person owns them instead."""
    from cron.jobs import OWNER_AUDIT_FILE_NAME

    script = _support._load_script()
    _root, profile = _support._personal_platform([_support.MALLORY])

    assert script.main(["--json", "--all-profiles"]) == 0
    report = _support.json.loads(capsys.readouterr().out)
    (store,) = [s_ for s_ in report["stores"] if s_["home"] == str(profile.resolve())]
    assert store["profile_people"] == [_support.MALLORY]
    assert {(j["id"], j["action"], j["new_owner"]) for j in store["jobs"]} == {
        ("agent3", "would_assign", _support.MALLORY),
        ("script3", "would_assign", _support.MALLORY),
    }

    assert script.main(["--apply", "--all-profiles"]) == 0
    assert "All clear" in capsys.readouterr().out
    assert _support._owners(profile) == {"agent3": _support.MALLORY, "script3": _support.MALLORY}
    rows = [_support.json.loads(line) for line in (profile / "cron" / OWNER_AUDIT_FILE_NAME).read_text().splitlines()]
    assert {r["new_owner"] for r in rows} == {_support.MALLORY}
    assert all(r["new_owner"] != _support.PRINCIPAL for r in rows)


def test_the_migration_leaves_a_profile_that_maps_to_several_people(capsys):
    script = _support._load_script()
    _root, profile = _support._personal_platform([_support.MALLORY, "bob@example.test"])

    assert script.main(["--all-profiles"]) == 1
    out = capsys.readouterr().out
    assert "agent3" in out and "several people" in out

    assert script.main(["--apply", "--all-profiles"]) == 1
    out = capsys.readouterr().out
    assert "Not all clear" in out and "All clear" not in out
    assert _support._owners(profile) == {"agent3": "", "script3": ""}


def test_the_migration_gives_a_job_to_the_governed_person_who_created_it(capsys):
    from cron.jobs import owner_audit_file

    script = _support._load_script()
    _support._root_store(
        [
            _support._raw_job("tasks1", origin=_support._webui_origin("Mallory@Example.Test")),
            _support._raw_job("legacy1"),
            _support._raw_job("admin1", origin=_support._webui_origin("root@example.test")),
            _support._raw_job("chat1", origin={"platform": "telegram", "chat_id": "42", "user_id": "12345"}),
        ],
        users={_support.MALLORY: {"roles": ["tech_lead"]}},
    )

    assert script.main(["--json"]) == 0
    (store,) = _support.json.loads(capsys.readouterr().out)["stores"]
    assert {(j["id"], j["action"], j["new_owner"]) for j in store["jobs"]} == {
        ("tasks1", "would_assign", _support.MALLORY),
        ("legacy1", "would_assign", _support.PRINCIPAL),
        ("admin1", "would_assign", _support.PRINCIPAL),
        ("chat1", "would_assign", _support.PRINCIPAL),
    }

    assert script.main(["--apply"]) == 0
    assert "All clear" in capsys.readouterr().out
    assert _support._owners(_support._home()) == {"tasks1": _support.MALLORY, "legacy1": _support.PRINCIPAL, "admin1": _support.PRINCIPAL, "chat1": _support.PRINCIPAL}
    rows = {r["job_id"]: r for r in map(_support.json.loads, owner_audit_file().read_text().splitlines())}
    assert "created" in rows["tasks1"]["reason"]


def test_the_migration_leaves_a_job_whose_creator_has_no_policy_entry(capsys):
    script = _support._load_script()
    _support._root_store(
        [_support._raw_job("tasks2", origin=_support._webui_origin("former@example.test"))],
        users={_support.MALLORY: {"roles": ["tech_lead"]}},
    )

    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert "tasks2" in out and "former@example.test" in out and "Not all clear" in out

    assert script.main(["--apply"]) == 1
    assert _support._owners(_support._home()) == {"tasks2": ""}
