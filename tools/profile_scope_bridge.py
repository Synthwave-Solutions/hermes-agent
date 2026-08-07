"""Shared bridge to the out-of-tree profile scope module.

``~/.hermes/profile_scope.py`` confines SCOPED freelancer profiles (e.g.
"steve") to their assigned project folders. This bridge is the single place
that loads it (mirroring how identity_tiers is kept outside the vendored
tree) and the single place that answers "is the current HERMES_HOME a scoped
profile's home?" for the fail-closed error paths in ``tools/approval.py``
and ``tools/file_tools.py``.

Everything here is fail-open for non-scoped profiles: on a workstation
without the scoping layer installed, ``load_profile_scope()`` returns None
and ``is_scoped_home()`` returns False, so guard behavior is unchanged.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Kept in sync with SCOPED_PROFILE_MARKER in ~/.hermes/profile_scope.py.
# Needed as a literal here because the marker fallback only matters when
# that module could not be loaded.
SCOPED_PROFILE_MARKER = ".profile-scoped"

_PROFILE_SCOPE_PATH = "~/.hermes/profile_scope.py"

_PROFILE_SCOPE_MOD = "__unloaded__"


def _real_home() -> str:
    """The account's passwd home, independent of the HOME env var.

    The profile multiplexer runs with HOME pointed at its own root
    (``~/.hermes-mux/home``), so plain ``expanduser`` would look for the
    scoping layer in a directory that does not exist and silently
    fail open for every multiplexed profile.
    """
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


def _expand(path: str) -> str:
    """``expanduser`` where ``~`` is the passwd home, never ``$HOME``."""
    if path == "~" or path.startswith("~/"):
        return _real_home() + path[1:]
    return os.path.expanduser(path)


def load_profile_scope():
    """Lazy-load ~/.hermes/profile_scope.py by absolute path. Cached.

    Returns the module, or None if it cannot be loaded. Logs at debug when
    the module file is simply absent (a workstation without the scoping
    layer) and at warning when the file exists but fails to load (a real
    problem worth surfacing).
    """
    global _PROFILE_SCOPE_MOD
    if _PROFILE_SCOPE_MOD == "__unloaded__":
        path = _expand(_PROFILE_SCOPE_PATH)
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("profile_scope", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _PROFILE_SCOPE_MOD = mod
        except Exception as e:
            if os.path.isfile(path):
                logger.warning("profile_scope load failed: %s", e)
            else:
                logger.debug("profile_scope module absent at %s: %s", path, e)
            _PROFILE_SCOPE_MOD = None
    return _PROFILE_SCOPE_MOD


def _reset_for_tests() -> None:
    """Clear the module cache so tests can re-drive the load path."""
    global _PROFILE_SCOPE_MOD
    _PROFILE_SCOPE_MOD = "__unloaded__"


def _profile_name_from_home(home: str) -> tuple[str | None, str]:
    """Parse ``(profile_name, profiles_root)`` out of a HERMES_HOME value.

    The name is derived by SEGMENT equality against
    ``<profiles_root>/<name>`` (never substring matching), so a sibling
    profile named e.g. "steven" is never conflated with "steve".
    """
    profiles_root = os.path.normpath(_expand("~/.hermes/profiles"))
    if not home:
        return None, profiles_root
    home_n = os.path.normpath(_expand(home))
    # The multiplexer serves profiles through a symlinked tree
    # (``~/.hermes-mux/profiles/<name>`` -> ``~/.hermes/profiles/<name>``),
    # so match the resolved path as well as the textual one.
    candidates = [home_n]
    try:
        real = os.path.realpath(home_n)
        if real != home_n:
            candidates.append(real)
    except OSError:
        pass
    for cand in candidates:
        if cand.startswith(profiles_root + os.sep):
            rest = cand[len(profiles_root) + 1:].split(os.sep)
            if rest and rest[0]:
                return rest[0], profiles_root
    return None, profiles_root


def is_scoped_home() -> bool:
    """True when HERMES_HOME points inside a SCOPED profile's home.

    Used by the guard error paths to decide fail-closed (scoped profile)
    versus fail-open (everyone else). When profile_scope.py loaded, its
    scoped set is authoritative. When it failed to load, fall back to the
    on-disk ``.profile-scoped`` marker file or the HERMES_PROFILE_SCOPED=1
    env override so a scoped profile still fails closed.
    """
    try:
        home = ""
        try:
            from hermes_constants import get_hermes_home_override
            home = get_hermes_home_override() or ""
        except Exception:
            home = ""
        name, profiles_root = _profile_name_from_home(
            home or os.environ.get("HERMES_HOME", ""))
        if not name:
            return False
        mod = load_profile_scope()
        if mod is not None:
            return name in mod.scoped_profiles()
        marker = os.path.join(profiles_root, name, SCOPED_PROFILE_MARKER)
        return (os.path.exists(marker)
                or os.environ.get("HERMES_PROFILE_SCOPED") == "1")
    except Exception:
        return False
