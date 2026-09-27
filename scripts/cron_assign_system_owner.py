#!/usr/bin/env python3
"""Give every ownerless cron job to the configured system principal.

Under governance ``enforce`` an agent cron job without an owner is refused at
fire time (``cron.scheduler._governed_as_job_owner``). Jobs made before owners
were recorded, and jobs an administrator or the CLI made, are ownerless. This
script assigns ``cron.system_principal`` to them so they keep running, under
that principal's governance.

DRY RUN BY DEFAULT. Without ``--apply`` it only reads (config.yaml, the
governance policy and cron/jobs.json of each store) and writes nothing at all:
no job, no lock file, no directory, no log. It prints what it would change and
whether ``--apply`` would be allowed.

With ``--apply`` each job is changed through ``cron.jobs.reassign_job_owner``:
only while the job is still ownerless (compare and set), refused inside a
governed non-admin session, and audited in ``cron/owner-audit.jsonl``.

Go-live order (see the program plan, runbook step 3): create the principal's
policy entry, set ``cron.system_principal`` in config.yaml, run this script
without ``--apply`` and read the report, then run it with ``--apply`` before
the restart, and check that no agent job is left without an owner.

Exit codes: 0 nothing blocks (dry run) or everything was assigned (apply);
1 a store has a blocking problem, or ownerless agent jobs are left after
apply; 2 usage error.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_REASON = "Assign the system principal to an ownerless job (go-live migration)"
SOURCE = "cron_assign_system_owner"


def _default_home() -> Path:
    raw = os.environ.get("HERMES_HOME", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def _bootstrap_imports() -> Path:
    """Prepare to import the engine that ships next to this script.

    Importing the engine is not side-effect free: ``hermes_cli.config`` runs
    plugin discovery at import time, which loads config.yaml and creates the
    home directory layout (and SOUL.md) under ``HERMES_HOME``. A dry run must
    write nothing to a real store, so the process ``HERMES_HOME`` points at a
    throwaway directory (removed on exit) and every real store is addressed
    by explicit path only (``use_cron_store`` and a home override).
    """
    sandbox = Path(tempfile.mkdtemp(prefix="cron-assign-owner-"))
    os.environ["HERMES_HOME"] = str(sandbox)
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    return sandbox


def _read_yaml(path: Path) -> Dict[str, Any]:
    import yaml

    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a mapping")
    return loaded


def _read_jobs(home: Path) -> List[Dict[str, Any]]:
    """Read cron/jobs.json without locks, repairs or directory creation."""
    from cron.jobs import _parse_jobs_file

    jobs_file = home / "cron" / "jobs.json"
    if not jobs_file.exists():
        return []
    data, _ = _parse_jobs_file(jobs_file)
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        if isinstance(jobs, dict):
            jobs = [{**v, "id": v.get("id") or k} for k, v in jobs.items() if isinstance(v, dict)]
    else:
        jobs = data
    if not isinstance(jobs, list):
        raise ValueError(f"{jobs_file} has an unexpected shape")
    return [job for job in jobs if isinstance(job, dict) and job.get("id")]


def _stores(args: argparse.Namespace, default_home: Path) -> List[Path]:
    homes = [Path(h).expanduser() for h in (args.hermes_home or [])] or [default_home]
    if args.all_profiles:
        for home in list(homes):
            profiles_root = home / "profiles"
            if profiles_root.is_dir():
                for profile in sorted(profiles_root.iterdir()):
                    if (profile / "cron" / "jobs.json").is_file():
                        homes.append(profile)
    unique: List[Path] = []
    for home in homes:
        resolved = home.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _principal_policy_entry(policy: Any, principal: str) -> str:
    """How the policy knows the principal: admin, restricted or missing."""
    from hermes_cli.dashboard_governance.models import GovernanceSubject
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access
    from hermes_cli.dashboard_governance.tool_policy import dwd_identity_for

    if principal not in policy.bootstrap_admins and principal not in policy.users:
        return "missing"
    access = resolve_effective_access(policy, GovernanceSubject(email=principal))
    return "admin" if dwd_identity_for(access) is None else "restricted"


def inspect_store(home: Path, *, principal_override: str, agent_only: bool) -> Dict[str, Any]:
    """Read one store and decide what ``--apply`` would do. Writes nothing."""
    from cron.jobs import system_principal_from_config
    from hermes_cli.dashboard_governance.loader import load_governance_policy

    report: Dict[str, Any] = {
        "home": str(home),
        "principal": "",
        "principal_source": "",
        "governance_mode": "",
        "principal_policy_entry": "",
        "jobs": [],
        "errors": [],
        "warnings": [],
    }
    try:
        config = _read_yaml(home / "config.yaml")
    except Exception as exc:
        report["errors"].append(f"config.yaml could not be read: {exc}")
        config = {}
    configured = system_principal_from_config(config)
    principal = principal_override or configured
    report["principal"] = principal
    report["principal_source"] = "--principal" if principal_override else ("config.yaml" if configured else "")
    if not principal:
        report["errors"].append(
            "No system principal: set cron.system_principal in config.yaml or pass --principal."
        )
    elif not configured:
        report["warnings"].append(
            "cron.system_principal is not set in config.yaml: new CLI and administrator "
            "jobs will stay ownerless and be refused under enforce."
        )
    elif principal != configured:
        report["warnings"].append(
            f"--principal {principal} differs from cron.system_principal {configured}."
        )

    try:
        policy = load_governance_policy(config=config, hermes_home=home)
    except Exception as exc:
        policy = None
        report["errors"].append(f"The governance policy could not be read: {exc}")
    if policy is not None:
        report["governance_mode"] = policy.mode
        if principal:
            entry = _principal_policy_entry(policy, principal)
            report["principal_policy_entry"] = entry
            if entry == "missing" and policy.mode == "enforce":
                report["errors"].append(
                    f"{principal} has no entry in the governance policy. Create it before "
                    "--apply, or every assigned job is refused everything it tries."
                )
            elif entry == "missing":
                report["warnings"].append(
                    f"{principal} has no entry in the governance policy; it is needed "
                    "before enforce is switched on."
                )
            elif entry == "restricted":
                report["warnings"].append(
                    f"{principal} is not an administrator in the policy: assigned jobs lose "
                    "unrestricted access, and delegated mailbox commands in their scripts "
                    "act as the principal only."
                )

    try:
        jobs = _read_jobs(home)
    except Exception as exc:
        report["errors"].append(f"cron/jobs.json could not be read: {exc}")
        jobs = []
    for job in jobs:
        if str(job.get("owner_email") or "").strip():
            continue
        kind = "script" if job.get("no_agent") else "agent"
        if agent_only and kind == "script":
            continue
        report["jobs"].append(
            {
                "id": str(job["id"]),
                "name": str(job.get("name") or ""),
                "kind": kind,
                "enabled": job.get("enabled", True) is not False,
                "action": "would_assign",
                "detail": "",
            }
        )
    return report


def apply_store(report: Dict[str, Any], *, actor: str, reason: str) -> None:
    """Assign the principal to every job in ``report`` (compare and set)."""
    from cron.jobs import reassign_job_owner, use_cron_store
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = Path(report["home"])
    token = set_hermes_home_override(str(home))
    try:
        with use_cron_store(home):
            for item in report["jobs"]:
                try:
                    result = reassign_job_owner(
                        item["id"],
                        report["principal"],
                        actor=actor,
                        reason=reason,
                        source=SOURCE,
                        expected_owner="",
                    )
                except ValueError as exc:
                    item["action"], item["detail"] = "skipped", str(exc)
                    continue
                if result is None:
                    item["action"], item["detail"] = "skipped", "the job no longer exists"
                else:
                    item["action"] = "assigned"
    finally:
        reset_hermes_home_override(token)


def _left_ownerless_agent_jobs(home: Path) -> List[str]:
    return [
        str(job["id"])
        for job in _read_jobs(home)
        if not str(job.get("owner_email") or "").strip() and not job.get("no_agent")
    ]


def _print_report(report: Dict[str, Any], *, apply: bool) -> None:
    print(f"Store: {report['home']}")
    source = f" (from {report['principal_source']})" if report["principal_source"] else ""
    print(f"  System principal: {report['principal'] or '(none)'}{source}")
    if report["governance_mode"]:
        entry = report["principal_policy_entry"] or "n/a"
        print(f"  Governance mode: {report['governance_mode']}; principal policy entry: {entry}")
    print(f"  Ownerless jobs: {len(report['jobs'])}")
    for item in report["jobs"]:
        state = "" if item["enabled"] else " (paused)"
        detail = f": {item['detail']}" if item["detail"] else ""
        print(f"    {item['id']}  {item['kind']:<6}  {item['name']!r}{state}  [{item['action']}{detail}]")
    for warning in report["warnings"]:
        print(f"  Warning: {warning}")
    for error in report["errors"]:
        print(f"  Blocking: {error}")
    if apply and "left_ownerless_agent_jobs" in report:
        print(f"  Ownerless agent jobs left: {len(report['left_ownerless_agent_jobs'])}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cron_assign_system_owner.py",
        description=(
            "Assign cron.system_principal to cron jobs that have no owner. "
            "Dry run unless --apply is given."
        ),
    )
    parser.add_argument(
        "--hermes-home",
        action="append",
        help="Store to process (repeatable; default: $HERMES_HOME or ~/.hermes)",
    )
    parser.add_argument(
        "--all-profiles",
        action="store_true",
        help="Also process every profile under <home>/profiles that has cron jobs",
    )
    parser.add_argument(
        "--principal",
        help="Use this address instead of cron.system_principal from config.yaml",
    )
    parser.add_argument(
        "--agent-only",
        action="store_true",
        help="Leave ownerless no_agent script jobs alone (they are not refused under enforce)",
    )
    parser.add_argument("--reason", default=DEFAULT_REASON, help="Reason kept in the audit log")
    parser.add_argument("--apply", action="store_true", help="Make the change (default: dry run)")
    parser.add_argument("--json", action="store_true", help="Print a JSON report")
    args = parser.parse_args(argv)

    default_home = _default_home()
    original_home = os.environ.get("HERMES_HOME")
    sandbox = _bootstrap_imports()
    try:
        return _run(args, default_home)
    finally:
        if original_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = original_home
        shutil.rmtree(sandbox, ignore_errors=True)


def _run(args: argparse.Namespace, default_home: Path) -> int:
    from cron.jobs import ensure_owner_admin_caller, normalize_owner_identity

    principal_override = ""
    if args.principal:
        try:
            principal_override = normalize_owner_identity(args.principal)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    reports = [
        inspect_store(home, principal_override=principal_override, agent_only=args.agent_only)
        for home in _stores(args, default_home)
    ]
    blocked = any(report["errors"] for report in reports)

    if args.apply:
        try:
            ensure_owner_admin_caller()
        except PermissionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if blocked:
            for report in reports:
                report["warnings"].append("Nothing was changed: fix the blocking problems first.")
        else:
            actor = f"os:{getpass.getuser() or 'unknown'}"
            for report in reports:
                apply_store(report, actor=actor, reason=str(args.reason or DEFAULT_REASON))
                report["left_ownerless_agent_jobs"] = _left_ownerless_agent_jobs(Path(report["home"]))

    left = any(report.get("left_ownerless_agent_jobs") for report in reports)
    ok = not blocked and not left
    if args.json:
        print(json.dumps({"mode": "apply" if args.apply else "dry_run", "ok": ok, "stores": reports}, indent=2))
    else:
        for report in reports:
            _print_report(report, apply=args.apply)
        if not args.apply:
            print("Dry run: nothing was changed. Run again with --apply to assign the owner.")
        elif blocked:
            print("Nothing was changed.")
        else:
            assigned = sum(1 for r in reports for item in r["jobs"] if item["action"] == "assigned")
            print(f"Assigned the system principal to {assigned} job(s). Audit: cron/owner-audit.jsonl")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
