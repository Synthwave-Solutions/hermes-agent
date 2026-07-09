from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from .loader import GovernancePolicyError, load_governance_policy, resolve_policy_path, save_governance_policy
from .models import GovernanceSubject
from .resolver import resolve_effective_access

_SAMPLE_POLICY: dict[str, Any] = {
    "version": 1,
    "mode": "report_only",
    "default_effect": "deny",
    "bootstrap_admins": ["admin@example.com"],
    "roles": {
        "admin": {
            "grants": {
                "permissions": ["*"],
                "profiles": ["*"],
                "routes": ["*"],
                "tools": {"toolsets": ["*"], "builtins": ["*"]},
                "skills": {"view": ["*"], "load": ["*"], "manage": ["*"]},
                "mcp": {"servers": ["*"], "tools": {"*": ["*"]}},
                "models": {"providers": ["*"], "models": ["*"]},
                "files": {"read_roots": ["~"], "write_roots": ["~"], "denied_globs": ["**/.env", "**/*secret*"]},
                "cli": {"commands": ["git", "python", "npm"], "workdir_roots": ["~"]},
                "usage_caps": {"daily_tool_calls": 500, "daily_background_processes": 20},
            }
        },
        "viewer": {
            "grants": {
                "permissions": ["sessions:read", "files:read", "logs:read", "status:read"],
                "profiles": ["default"],
                "routes": ["/api/sessions*", "/api/files*", "/api/logs*", "/api/status"],
            }
        },
    },
    "users": {
        "admin@example.com": {"roles": ["admin"]},
    },
}


def _serialize_effective_access(access) -> dict[str, Any]:
    subject = access.subject
    return {
        "mode": access.mode,
        "subject": {
            "email": subject.email,
            "display_name": subject.display_name,
            "provider": subject.provider,
            "user_id": subject.user_id,
            "org_id": subject.org_id,
        },
        "roles": sorted(access.roles),
        "groups": sorted(access.groups),
        "permissions": sorted(access.permissions),
        "profiles": sorted(access.profiles),
        "routes": sorted(access.routes),
        "grant_sources": list(access.grant_sources),
        "is_admin": access.has_permission("governance:read") or access.has_permission("governance:write"),
    }


def _policy_path(args: argparse.Namespace) -> Path:
    if getattr(args, "policy", None):
        return Path(args.policy).expanduser()
    return resolve_policy_path()


def cmd_init(args: argparse.Namespace) -> int:
    path = _policy_path(args)
    if path.exists() and not getattr(args, "force", False):
        print(f"Policy already exists: {path}")
        print("Use --force to overwrite.")
        return 1
    save_governance_policy(_SAMPLE_POLICY, path=path)
    print(f"Wrote sample dashboard governance policy: {path}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = _policy_path(args)
    try:
        policy = load_governance_policy(path=path)
    except GovernancePolicyError as exc:
        print(f"invalid: {exc}")
        return 1
    print(f"valid: {path}")
    print(f"mode: {policy.mode}")
    print(f"roles: {len(policy.roles)} groups: {len(policy.groups)} users: {len(policy.users)}")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    path = _policy_path(args)
    try:
        policy = load_governance_policy(path=path)
    except GovernancePolicyError as exc:
        print(f"invalid: {exc}")
        return 1
    subject = GovernanceSubject(
        email=args.email or "",
        user_id=args.user_id or args.email or "",
        provider=args.provider or "cli",
        roles=tuple(args.role or ()),
        groups=tuple(args.group or ()),
    )
    access = resolve_effective_access(policy, subject)
    print(json.dumps(_serialize_effective_access(access), indent=2, sort_keys=True))
    return 0


def cmd_export_sample(args: argparse.Namespace) -> int:
    print(yaml.safe_dump(_SAMPLE_POLICY, sort_keys=False, default_flow_style=False))
    return 0


def register_cli(subparsers) -> None:
    parser = subparsers.add_parser(
        "governance",
        help="Manage dashboard governance RBAC policy",
        description="Initialize, validate, and preview dashboard governance policy files.",
    )
    subs = parser.add_subparsers(dest="governance_command")

    init = subs.add_parser("init", help="Write a sample report-only governance policy")
    init.add_argument("--policy", help="Policy file path (default: ~/.hermes/dashboard-governance.yaml)")
    init.add_argument("--force", action="store_true", help="Overwrite an existing policy file")
    init.set_defaults(func=cmd_init)

    validate = subs.add_parser("validate", help="Validate a governance policy file")
    validate.add_argument("--policy", help="Policy file path")
    validate.set_defaults(func=cmd_validate)

    preview = subs.add_parser("preview", help="Preview effective access for a subject")
    preview.add_argument("--policy", help="Policy file path")
    preview.add_argument("--email", default="", help="Subject email")
    preview.add_argument("--user-id", default="", help="Subject user id")
    preview.add_argument("--provider", default="cli", help="Subject provider")
    preview.add_argument("--role", action="append", help="Transient role claim (repeatable)")
    preview.add_argument("--group", action="append", help="Transient group claim (repeatable)")
    preview.set_defaults(func=cmd_preview)

    sample = subs.add_parser("sample", help="Print the built-in sample policy YAML")
    sample.set_defaults(func=cmd_export_sample)

    parser.set_defaults(func=lambda args: (parser.print_help() or 0))
