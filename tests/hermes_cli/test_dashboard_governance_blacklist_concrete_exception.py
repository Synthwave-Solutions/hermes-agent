"""An approved concrete path reopens a specific deny for a blacklist account; wildcards never do."""
import yaml

from hermes_cli.dashboard_governance.loader import load_governance_policy
from hermes_cli.dashboard_governance.models import GovernanceSubject
from hermes_cli.dashboard_governance.resolver import resolve_effective_access
from hermes_cli.dashboard_governance.tool_policy import decide_tool_argument_access

CLIENTS = "/srv/cfg/clients"


def _access(tmp_path, allow):
    policy = {"mode": "enforce", "users": {"a@example.test": {
        "access_level": "admin", "access_mode": "blacklist",
        "grants": {"cli": {"commands": ["*"]}, "files": {
            "denied_globs": [CLIENTS, f"{CLIENTS}/**"], "allow_globs": allow}}}}}
    path = tmp_path / "gov.yaml"
    path.write_text(yaml.safe_dump(policy))
    return resolve_effective_access(load_governance_policy(path=path), GovernanceSubject(email="a@example.test"))


def _allowed(access, tool, **args):
    return decide_tool_argument_access(access, tool, args).allowed


def test_approved_folder_opens_its_files_in_terminal_and_file_tools(tmp_path):
    access = _access(tmp_path, [f"{CLIENTS}/ivcb", f"{CLIENTS}/ivcb/**"])
    assert _allowed(access, "terminal", command=f"ls {CLIENTS}/ivcb/")
    assert _allowed(access, "terminal", command=f"AZURE_CONFIG_DIR={CLIENTS}/ivcb az account show")
    assert _allowed(access, "read_file", path=f"{CLIENTS}/ivcb/session.json")


def test_other_client_folders_stay_denied(tmp_path):
    access = _access(tmp_path, [f"{CLIENTS}/ivcb", f"{CLIENTS}/ivcb/**"])
    assert not _allowed(access, "terminal", command=f"cat {CLIENTS}/acme/creds.json")
    assert not _allowed(access, "read_file", path=f"{CLIENTS}/ivcb-other/creds.json")


def test_wildcard_exception_never_reopens_a_specific_deny(tmp_path):
    access = _access(tmp_path, ["**/*.json", f"{CLIENTS}/*/**"])
    assert not _allowed(access, "read_file", path=f"{CLIENTS}/acme/creds.json")


def test_bare_folder_exception_covers_only_the_folder_itself(tmp_path):
    access = _access(tmp_path, [f"{CLIENTS}/ivcb"])
    assert not _allowed(access, "read_file", path=f"{CLIENTS}/ivcb/session.json")


def test_name_and_type_denies_stay_closed_inside_an_approved_folder(tmp_path):
    access = _access(tmp_path, [f"{CLIENTS}/ivcb", f"{CLIENTS}/ivcb/**"])
    raw = access.grants.file_denied_globs | {"**/.env", "*bunq*"}
    from dataclasses import replace
    access = replace(access, grants=replace(access.grants, file_denied_globs=frozenset(raw)))
    assert _allowed(access, "read_file", path=f"{CLIENTS}/ivcb/session.json")
    assert not _allowed(access, "read_file", path=f"{CLIENTS}/ivcb/.env")
    assert not _allowed(access, "read_file", path=f"{CLIENTS}/ivcb/bunq.json")
