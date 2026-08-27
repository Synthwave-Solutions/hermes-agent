from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

import yaml

from hermes_cli.config import get_hermes_home, load_config

from .models import (
    GrantSet,
    GovernanceGroup,
    GovernancePolicy,
    GovernanceRole,
    GovernanceUser,
    _ALLOWED_MODES,
    _norm_email,
    _string_set,
)


class GovernancePolicyError(ValueError):
    """Raised when a dashboard governance policy is invalid."""


def resolve_policy_path(
    *,
    config: Mapping[str, Any] | None = None,
    hermes_home: str | Path | None = None,
) -> Path:
    cfg = dict(config) if isinstance(config, Mapping) else None
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
    dash = cfg.get("dashboard") if isinstance(cfg.get("dashboard"), Mapping) else {}
    gov = dash.get("governance") if isinstance(dash.get("governance"), Mapping) else {}
    raw = gov.get("policy_file") if isinstance(gov, Mapping) else None
    if raw:
        return Path(str(raw)).expanduser()
    home = Path(hermes_home).expanduser() if hermes_home is not None else get_hermes_home()
    return home / "dashboard-governance.yaml"


def _parse_role(name: str, raw: Any) -> GovernanceRole:
    data = raw if isinstance(raw, Mapping) else {}
    return GovernanceRole(
        name=name,
        description=str(data.get("description") or ""),
        grants=GrantSet.from_mapping(data.get("grants") if isinstance(data.get("grants"), Mapping) else data),
    )


def _parse_group(name: str, raw: Any) -> GovernanceGroup:
    data = raw if isinstance(raw, Mapping) else {}
    return GovernanceGroup(
        name=name,
        description=str(data.get("description") or ""),
        roles=tuple(str(v) for v in (data.get("roles") or []) if str(v).strip()),
        sso_groups=_string_set(data.get("sso_groups")),
        grants=GrantSet.from_mapping(data.get("grants")),
        deny=GrantSet.from_mapping(data.get("deny")),
    )


def _parse_user(email: str, raw: Any) -> GovernanceUser:
    data = raw if isinstance(raw, Mapping) else {}
    return GovernanceUser(
        email=_norm_email(email),
        roles=tuple(str(v) for v in (data.get("roles") or []) if str(v).strip()),
        groups=tuple(str(v) for v in (data.get("groups") or []) if str(v).strip()),
        grants=GrantSet.from_mapping(data.get("grants")),
        deny=GrantSet.from_mapping(data.get("deny")),
    )


def parse_governance_policy(data: Mapping[str, Any] | None) -> GovernancePolicy:
    raw = dict(data or {})
    mode = str(raw.get("mode") or "off").strip().lower()
    if mode not in _ALLOWED_MODES:
        raise GovernancePolicyError(f"invalid dashboard governance mode: {mode}")
    default_effect = str(raw.get("default_effect") or "deny").strip().lower()
    if default_effect != "deny":
        raise GovernancePolicyError("dashboard governance v1 only supports default_effect: deny")
    roles_raw = raw.get("roles") if isinstance(raw.get("roles"), Mapping) else {}
    groups_raw = raw.get("groups") if isinstance(raw.get("groups"), Mapping) else {}
    users_raw = raw.get("users") if isinstance(raw.get("users"), Mapping) else {}
    roles = {str(name): _parse_role(str(name), value) for name, value in roles_raw.items()}
    groups = {str(name): _parse_group(str(name), value) for name, value in groups_raw.items()}
    users = {_norm_email(str(email)): _parse_user(str(email), value) for email, value in users_raw.items()}
    return GovernancePolicy(
        version=int(raw.get("version") or 1),
        mode=mode,
        default_effect=default_effect,
        bootstrap_admins=tuple(sorted(_norm_email(v) for v in raw.get("bootstrap_admins") or [] if _norm_email(v))),
        roles=roles,
        groups=groups,
        users=users,
        raw=raw,
    )


def load_governance_policy(
    *,
    path: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
    hermes_home: str | Path | None = None,
) -> GovernancePolicy:
    policy_path = Path(path).expanduser() if path is not None else resolve_policy_path(config=config, hermes_home=hermes_home)
    if not policy_path.exists():
        return GovernancePolicy(mode="off", default_effect="deny")
    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise GovernancePolicyError(f"invalid YAML in dashboard governance policy: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise GovernancePolicyError("dashboard governance policy must be a mapping")
    return parse_governance_policy(loaded)


def save_governance_policy(
    data: Mapping[str, Any],
    *,
    path: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
    hermes_home: str | Path | None = None,
) -> Path:
    """Validate and atomically write dashboard governance policy YAML."""
    if not isinstance(data, Mapping):
        raise GovernancePolicyError("dashboard governance policy must be a mapping")
    parse_governance_policy(data)
    policy_path = Path(path).expanduser() if path is not None else resolve_policy_path(config=config, hermes_home=hermes_home)
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name per writer: with a fixed ``<policy>.yaml.tmp`` two
    # concurrent writers (the dashboard and the ``hermes governance`` CLI, or
    # two dashboard workers) truncate the SAME temp file mid-write and can
    # publish interleaved content. mkstemp gives each writer its own file, so
    # concurrent saves degrade to last-write-wins of complete snapshots.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(policy_path.parent),
        prefix=f".{policy_path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(data), handle, sort_keys=False, default_flow_style=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, policy_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return policy_path
