"""Personal vault scope for authenticated, multi-user surfaces.

Bot configuration remains profile-scoped. Only password stores, manager homes
and unlock tokens follow the authenticated person. Context-copying workers and
delegated agents inherit this scope without modifying the process environment.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
import os
from pathlib import Path

_personal_home: ContextVar[Path | None] = ContextVar("personal_vault_home", default=None)


def bind_personal_vault(home: Path) -> Token:
    root = Path(home).absolute()
    _safe(root)
    return _personal_home.set(root)


def reset_personal_vault(token: Token) -> None:
    _personal_home.reset(token)


def personal_vault_bound() -> bool:
    return _personal_home.get() is not None


def _safe(path: Path) -> Path:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise PermissionError("Personal vault paths cannot contain symlinks")
    return path


def vault_home() -> Path:
    root = _personal_home.get()
    if root is not None:
        return _safe(root)
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def personal_config() -> dict | None:
    if not personal_vault_bound():
        return None
    from agent.vault_store import get_vault_store
    return get_vault_store().read_source_settings()


def personal_source_snapshot(settings: dict) -> tuple[Path, dict] | None:
    """Pin a backend to the personal source version that created it."""
    return (vault_home(), dict(settings)) if personal_vault_bound() else None


def require_personal_source(name: str, snapshot: tuple[Path, dict] | None) -> dict | None:
    """Recheck revocation at a manager call, including a replaced identical account."""
    if snapshot is None and not personal_vault_bound():
        return None
    if snapshot is None or not personal_vault_bound() or vault_home() != snapshot[0]:
        raise PermissionError('The personal password manager source is no longer available.')
    current = (personal_config() or {}).get(name)
    if (not isinstance(current, dict) or current.get('enabled') is False
            or current.get('connected') is not True or current != snapshot[1]):
        raise PermissionError('The personal password manager source is no longer available.')
    return current


def manager_environment() -> dict[str, str] | None:
    if not personal_vault_bound():
        return None
    root = vault_home() / "password-managers"
    directories = {"HOME": root, "XDG_CONFIG_HOME": root / ".config",
                   "OP_CONFIG_DIR": root / "onepassword",
                   "BITWARDENCLI_APPDATA_DIR": root / "bitwarden"}
    for path in directories.values():
        _safe(path).mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    # Never inherit host manager credentials, account selection, desktop IPC,
    # or platform-specific home aliases from the service's launch environment.
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "LANG", "LC_ALL") if key in os.environ}
    env.update({key: str(path) for key, path in directories.items()})
    env.update(NO_COLOR="1", OP_BIOMETRIC_UNLOCK_ENABLED="false",
               OP_LOAD_DESKTOP_APP_SETTINGS="false", OP_CACHE="false")
    return env
