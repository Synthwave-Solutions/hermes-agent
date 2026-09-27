#!/usr/bin/env python3
"""Give every ownerless cron job an owner: the system principal, or the person.

Under governance ``enforce`` an agent cron job without an owner is refused at
fire time (``cron.scheduler._governed_as_job_owner``). Jobs made before owners
were recorded, and jobs an administrator or the CLI made, are ownerless. This
script assigns ``cron.system_principal`` to them so they keep running, under
that principal's governance.

A governed person's own profile never gets the principal: that would run
their jobs with administrator rights. When the governance policy gives a
named profile, by name, to exactly one person who is not an administrator,
its ownerless jobs go to that person. When it gives it to several, they are
left ownerless and listed for an administrator to decide
(``hermes cron reassign-owner``).

DRY RUN BY DEFAULT. Without ``--apply`` it only reads (config.yaml, the
governance policy and cron/jobs.json of each store) and writes nothing at all:
no job, no lock file, no directory, no log. It prints what it would change and
whether ``--apply`` would be allowed.

With ``--apply`` each job is changed through ``cron.jobs.reassign_job_owner``:
only while the job is still ownerless (compare and set), refused inside a
governed non-admin session, and audited in ``cron/owner-audit.jsonl``.

Only the selected stores are changed: ``--hermes-home`` (default
``$HERMES_HOME`` or ``~/.hermes``), plus every named profile store with
``--all-profiles``. Every other store of the same platform (its root and all
its named profiles) is still read, and ownerless agent jobs there are
reported: the script never says all clear while any store has one.

Go-live order (see the program plan, runbook step 3): create the principal's
policy entry, set ``cron.system_principal`` in config.yaml, run this script
without ``--apply`` and read the report, then run it with ``--apply`` before
the restart, and check that it ends with "All clear". Use ``--all-profiles``
so named profile stores are covered too: a profile that sets no
``cron.system_principal`` and no governance policy of its own uses the
platform root's, the same rule its fires follow.

Exit codes: 0 nothing blocks and no agent job would be left, or is left,
without an owner in any store of the platform (dry run and apply alike);
1 a store has a blocking problem, or an ownerless agent job would be left
or is left somewhere (a store that was not selected, or a profile shared by
several people); 2 usage error.
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
PERSON_REASON = "Assign the person whose own profile holds this ownerless job (go-live migration)"
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


def _profile_stores(root: Path) -> List[Path]:
    """The named profile stores under ``root`` that have cron jobs."""
    profiles_root = root / "profiles"
    if not profiles_root.is_dir():
        return []
    return [
        profile
        for profile in sorted(profiles_root.iterdir())
        if not profile.name.startswith(".") and (profile / "cron" / "jobs.json").is_file()
    ]


def _unique(homes: List[Path]) -> List[Path]:
    unique: List[Path] = []
    for home in homes:
        resolved = home.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _stores(args: argparse.Namespace, default_home: Path) -> List[Path]:
    """The stores this run may change."""
    homes = [Path(h).expanduser() for h in (args.hermes_home or [])] or [default_home]
    if args.all_profiles:
        for home in list(homes):
            homes.extend(_profile_stores(home))
    return _unique(homes)


def _other_platform_stores(selected: List[Path]) -> List[Path]:
    """Every store of the selected stores' platforms that was not selected.

    The platform of a store is its root home and all of the root's named
    profiles; the root of a named profile is found the way its fires find it
    (``cron.jobs.platform_root_for``).
    """
    from cron.jobs import platform_root_for

    candidates: List[Path] = []
    for home in selected:
        root = platform_root_for(home) or home
        candidates.append(root)
        candidates.extend(_profile_stores(root))
    return [home for home in _unique(candidates) if home not in selected]


def _principal_policy_entry(policy: Any, principal: str) -> str:
    """How the policy knows the principal: admin, restricted or missing."""
    from hermes_cli.dashboard_governance.models import GovernanceSubject
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access
    from hermes_cli.dashboard_governance.tool_policy import dwd_identity_for

    if principal not in policy.bootstrap_admins and principal not in policy.users:
        return "missing"
    access = resolve_effective_access(policy, GovernanceSubject(email=principal))
    return "admin" if dwd_identity_for(access) is None else "restricted"


def _profile_people(policy: Any, profile: str) -> List[str]:
    """The governed people the policy gives ``profile`` to, by its name.

    Only people who are not administrators count (an administrator's jobs
    may run with administrator rights anyway), and only a grant of this
    exact profile name: a wildcard or a pattern gives many profiles and makes
    none of them anyone's own.
    """
    from hermes_cli.dashboard_governance.models import GovernanceSubject
    from hermes_cli.dashboard_governance.resolver import resolve_effective_access
    from hermes_cli.dashboard_governance.tool_policy import dwd_identity_for

    people: List[str] = []
    for email in sorted(policy.users):
        access = resolve_effective_access(policy, GovernanceSubject(email=email))
        if dwd_identity_for(access) is None:  # None means administrator
            continue
        if profile in access.profiles and access.is_profile_allowed(profile):
            people.append(email)
    return people


def inspect_store(home: Path, *, principal_override: str, agent_only: bool) -> Dict[str, Any]:
    """Read one store and decide what ``--apply`` would do. Writes nothing.

    A named profile store that sets neither a principal nor a policy of its
    own follows the platform root's (``cron.jobs.cron_system_principal`` and
    ``cron.jobs.resolve_cron_policy_path``), exactly as its fires do.

    In a named profile the policy gives to governed people by name
    (``_profile_people``) the principal is never assigned: one person gets
    the jobs, several leave them ownerless ("left").
    """
    from cron.jobs import (
        load_cron_governance_policy,
        platform_root_for,
        read_config_file,
        resolve_cron_policy_path,
        system_principal_from_config,
    )
    from hermes_cli.dashboard_governance.loader import load_governance_policy

    report: Dict[str, Any] = {
        "home": str(home),
        "principal": "",
        "principal_source": "",
        "governance_mode": "",
        "policy_file": "",
        "policy_source": "",
        "principal_policy_entry": "",
        "profile_people": [],
        "jobs": [],
        "errors": [],
        "warnings": [],
    }
    # Problems with the principal block only a store that would assign it.
    principal_errors: List[str] = []
    root = platform_root_for(home)
    try:
        config = _read_yaml(home / "config.yaml")
    except Exception as exc:
        report["errors"].append(f"config.yaml could not be read: {exc}")
        config = {}
    configured = system_principal_from_config(config)
    configured_source = "config.yaml" if configured else ""
    if not configured and root is not None:
        try:
            configured = system_principal_from_config(read_config_file(root / "config.yaml"))
        except ValueError as exc:
            report["errors"].append(f"The platform config.yaml could not be read: {exc}")
        configured_source = "platform config.yaml" if configured else ""
    principal = principal_override or configured
    report["principal"] = principal
    report["principal_source"] = "--principal" if principal_override else configured_source
    if not principal:
        principal_errors.append(
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

    policy = None
    policy_path: Optional[Path] = None
    try:
        policy_path, report["policy_source"] = resolve_cron_policy_path(hermes_home=home, config=config)
        report["policy_file"] = str(policy_path)
        policy = load_governance_policy(path=policy_path)
    except Exception as exc:
        report["errors"].append(f"The governance policy could not be read: {exc}")
    if policy is not None:
        report["governance_mode"] = policy.mode
        if principal:
            entry = _principal_policy_entry(policy, principal)
            report["principal_policy_entry"] = entry
            if entry == "missing" and policy.mode == "enforce":
                principal_errors.append(
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
    platform_policy = policy if report["policy_source"] == "platform" else None
    if policy is not None and root is not None and report["policy_source"] == "store":
        # A profile with a policy of its own: say so when it is weaker than
        # the platform's. (A policy file it names that does not exist already
        # failed above: its fires refuse, cron.jobs.resolve_cron_policy_path.)
        try:
            platform_policy = load_cron_governance_policy(
                hermes_home=root, config=read_config_file(root / "config.yaml")
            )
        except Exception as exc:
            report["errors"].append(f"The platform governance policy could not be read: {exc}")
        else:
            if platform_policy.mode == "enforce" and policy.mode != "enforce":
                report["warnings"].append(
                    f"This profile has its own policy in mode {policy.mode} while the "
                    "platform policy is enforced."
                )

    # Whose own profile this is: the people are defined in the platform
    # policy, and a profile with a policy of its own may name them too.
    people: List[str] = []
    if policy is not None and root is not None:
        try:
            for known in (policy, platform_policy):
                if known is not None:
                    people.extend(p for p in _profile_people(known, home.name) if p not in people)
        except Exception as exc:
            report["errors"].append(f"Who this profile belongs to could not be read: {exc}")
    people.sort()
    report["profile_people"] = people

    try:
        jobs = _read_jobs(home)
    except Exception as exc:
        report["errors"].append(f"cron/jobs.json could not be read: {exc}")
        jobs = []
    if len(people) == 1:
        new_owner, action, detail = people[0], "would_assign", ""
    elif people:
        new_owner, action = "", "left"
        detail = (
            "this profile belongs to several people ("
            + ", ".join(people)
            + "): an administrator must choose with hermes cron reassign-owner"
        )
    else:
        new_owner, action, detail = principal, "would_assign", ""
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
                "action": action,
                "new_owner": new_owner,
                "detail": detail,
            }
        )
    if people:
        report["warnings"].extend(principal_errors)
        if len(people) > 1:
            report["warnings"].append(
                f"The policy gives this profile to several people ({', '.join(people)}); "
                "its ownerless jobs are left for an administrator to assign."
            )
    elif report["jobs"]:
        report["errors"].extend(principal_errors)
    else:
        report["warnings"].extend(principal_errors)
    return report


def inspect_other_store(home: Path) -> Dict[str, Any]:
    """Read a store this run does not change: its ownerless agent jobs."""
    report: Dict[str, Any] = {"home": str(home), "ownerless_agent_jobs": [], "errors": []}
    try:
        report["ownerless_agent_jobs"] = _left_ownerless_agent_jobs(home)
    except Exception as exc:
        report["errors"].append(f"cron/jobs.json could not be read: {exc}")
    return report


def apply_store(report: Dict[str, Any], *, actor: str, reason: str) -> None:
    """Give every job in ``report`` its new owner (compare and set).

    ``reason`` is kept for jobs that go to the principal; a job that goes to
    the person whose profile holds it keeps ``PERSON_REASON`` unless the
    operator passed a reason of their own. Jobs ``left`` are not touched.
    """
    from cron.jobs import reassign_job_owner, use_cron_store
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = Path(report["home"])
    token = set_hermes_home_override(str(home))
    try:
        with use_cron_store(home):
            for item in report["jobs"]:
                if item["action"] != "would_assign" or not item.get("new_owner"):
                    continue
                to_person = item["new_owner"] != report["principal"]
                try:
                    result = reassign_job_owner(
                        item["id"],
                        item["new_owner"],
                        actor=actor,
                        reason=PERSON_REASON if to_person and reason == DEFAULT_REASON else reason,
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
    if report["policy_file"]:
        origin = " (platform policy)" if report["policy_source"] == "platform" else ""
        print(f"  Governance policy: {report['policy_file']}{origin}")
    if report["governance_mode"]:
        entry = report["principal_policy_entry"] or "n/a"
        print(f"  Governance mode: {report['governance_mode']}; principal policy entry: {entry}")
    if report.get("profile_people"):
        print(f"  Profile of: {', '.join(report['profile_people'])}")
    print(f"  Ownerless jobs: {len(report['jobs'])}")
    for item in report["jobs"]:
        state = "" if item["enabled"] else " (paused)"
        owner = f" to {item['new_owner']}" if item.get("new_owner") else ""
        detail = f": {item['detail']}" if item["detail"] else ""
        print(f"    {item['id']}  {item['kind']:<6}  {item['name']!r}{state}  [{item['action']}{owner}{detail}]")
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


def _agent_jobs_left_behind(reports: List[Dict[str, Any]], others: List[Dict[str, Any]]) -> List[str]:
    """Where agent jobs without an owner remain, or would remain after apply.

    After an apply that ran: what the store holds now. Otherwise (a dry run,
    or an apply that blocking problems stopped): every ownerless agent job
    that apply would not, or could not, give an owner.
    """
    places: List[str] = []
    for report in reports:
        if "left_ownerless_agent_jobs" in report:
            ids = report["left_ownerless_agent_jobs"]
        else:
            ids = [
                item["id"]
                for item in report["jobs"]
                if item["kind"] == "agent"
                and (report["errors"] or item["action"] != "would_assign" or not item.get("new_owner"))
            ]
        if ids:
            places.append(f"{report['home']}: {', '.join(ids)}")
    for other in others:
        if other["ownerless_agent_jobs"]:
            places.append(
                f"{other['home']} (not selected; use --all-profiles or --hermes-home): "
                + ", ".join(other["ownerless_agent_jobs"])
            )
    return places


def _run(args: argparse.Namespace, default_home: Path) -> int:
    from cron.jobs import ensure_owner_admin_caller, normalize_owner_identity

    principal_override = ""
    if args.principal:
        try:
            principal_override = normalize_owner_identity(args.principal)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    selected = _stores(args, default_home)
    reports = [
        inspect_store(home, principal_override=principal_override, agent_only=args.agent_only)
        for home in selected
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

    # Read the other stores of the platform last, so a store that apply just
    # changed is never reported from before the change.
    others = [inspect_other_store(home) for home in _other_platform_stores(selected)]
    uncovered = [other for other in others if other["ownerless_agent_jobs"] or other["errors"]]
    left_behind = _agent_jobs_left_behind(reports, uncovered)
    unreadable = any(other["errors"] for other in uncovered)
    ok = not blocked and not left_behind and not unreadable
    if args.json:
        print(json.dumps(
            {
                "mode": "apply" if args.apply else "dry_run",
                "ok": ok,
                "stores": reports,
                "uncovered_stores": uncovered,
            },
            indent=2,
        ))
        return 0 if ok else 1

    for report in reports:
        _print_report(report, apply=args.apply)
    for other in uncovered:
        print(f"Store not selected: {other['home']}")
        for error in other["errors"]:
            print(f"  Blocking: {error}")
        if other["ownerless_agent_jobs"]:
            print(
                f"  Agent jobs without an owner: {', '.join(other['ownerless_agent_jobs'])} "
                "(run with --all-profiles, or --hermes-home for this store)"
            )
    if not args.apply:
        print("Dry run: nothing was changed. Run again with --apply to assign the owner.")
    elif blocked:
        print("Nothing was changed.")
    else:
        assigned = [(r, item) for r in reports for item in r["jobs"] if item["action"] == "assigned"]
        to_principal = sum(1 for r, item in assigned if item["new_owner"] == r["principal"])
        to_person = len(assigned) - to_principal
        people = f" and the person of a personal profile to {to_person} job(s)" if to_person else ""
        print(
            f"Assigned the system principal to {to_principal} job(s){people}. "
            "Audit: cron/owner-audit.jsonl"
        )
    if ok:
        if args.apply:
            print("All clear: no agent job is left without an owner in any store.")
        else:
            print("Nothing blocks: --apply would leave no agent job without an owner in any store.")
    else:
        if args.apply:
            print("Not all clear: agent jobs without an owner remain:")
        else:
            print("Not all clear: --apply would leave agent jobs without an owner:")
        for place in left_behind:
            print(f"  {place}")
        if blocked:
            print("  (and a store has a blocking problem, see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
