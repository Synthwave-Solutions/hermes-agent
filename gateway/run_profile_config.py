"""Run profile config.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gateway.config import GatewayConfig

def _synthpulse_root_shared_credential(adapter: Any, profile_home: Any = None) -> bool:
    """True when the adapter's credential equals one in the root profile's .env
    while this process is NOT the root profile's gateway (multiplexer next to
    the main gateway). The root is derived from the REAL profile directory
    (<root>/profiles/<name>, symlinks resolved), because a multiplexer runs
    under its own HERMES_HOME. Compared by the same fingerprint the duplicate
    detection uses, so plugin adapters that keep the token elsewhere are
    covered. Fail-open: any error means "not shared"."""
    from gateway.run import (
        GatewayRunner, Path, _ROOT_CREDENTIAL_CACHE, _ROOT_CREDENTIAL_VARS, logger
    )
    try:
        from hermes_constants import get_hermes_home
        real = Path(profile_home).resolve() if profile_home else None
        if real is None or real.parent.name != "profiles":
            return False
        root = real.parent.parent
        if Path(get_hermes_home()).resolve() == root.resolve():
            return False
        env_path = root / ".env"
        st = env_path.stat()
        if _ROOT_CREDENTIAL_CACHE["mtime"] != st.st_mtime_ns:
            from agent.secret_scope import load_env_file
            import hashlib
            values = load_env_file(env_path)
            _ROOT_CREDENTIAL_CACHE["values"] = {
                hashlib.sha256(("hermes-mux:" + str(values.get(k) or "").strip()).encode("utf-8")).hexdigest()[:16]
                for k in _ROOT_CREDENTIAL_VARS if str(values.get(k) or "").strip()
            }
            _ROOT_CREDENTIAL_CACHE["mtime"] = st.st_mtime_ns
        fingerprint = GatewayRunner._adapter_credential_fingerprint(adapter)
        shared = bool(fingerprint) and fingerprint in _ROOT_CREDENTIAL_CACHE["values"]
        logger.info("[MULTIPLEX] root-credential guard: profile_home=%s root=%s shared=%s", profile_home, root, shared)
        return shared
    except Exception as boundary_error:
        import logging
        logging.getLogger(__name__).error("Runtime boundary failed",
            exc_info=(RuntimeError, RuntimeError(type(boundary_error).__name__), boundary_error.__traceback__))
        return False


def load_gateway_config_for_runner() -> GatewayConfig:
    """Load gateway config for the process-level GatewayRunner. An UNSET ``multiplex_profiles`` is
    settled first by ``resolve_multiplex_mode`` (the default is on; the boot guard keeps a fleet that
    still runs per-profile gateways standalone). Multiplexed: set multiplex-active, then reload
    under the default root's ``_profile_runtime_scope`` — not ``get_hermes_home()``, which is the
    named launcher when a profile-scoped process started the host. A scope miss must not fall
    through to that process's ``os.environ``. Off -> identical to ``load_gateway_config()``.

    See #64674.

    The probe load above only decides whether this process is a multiplexer. The primary
    config is the scoped reload: multiplex must already be active (a scope miss must not
    fall through to the launching profile's ``os.environ``) and the home must be the
    default root, not ``get_hermes_home()`` — a named launcher's home is not the owner
    of the primary adapter map.
    """
    from gateway.run import (
        Path, _profile_runtime_scope, load_gateway_config, logger
    )
    from hermes_cli.gateway_multiplex_mode import log_multiplex_decision, resolve_multiplex_mode
    cfg = load_gateway_config()
    log_multiplex_decision(resolve_multiplex_mode(cfg))
    if not cfg.multiplex_profiles:
        return cfg
    try:
        from agent.secret_scope import set_multiplex_active
        set_multiplex_active(True)
    except Exception as boundary_error:
        import logging
        logging.getLogger(__name__).error("Runtime boundary failed",
            exc_info=(RuntimeError, RuntimeError(type(boundary_error).__name__), boundary_error.__traceback__))
    try:
        from hermes_constants import get_default_hermes_root
        home = get_default_hermes_root()
    except (ImportError, OSError, ValueError):
        logger.exception("Could not resolve the multiplex default home")
        return cfg
    try:
        with _profile_runtime_scope(Path(home)):
            scoped = load_gateway_config()
    except Exception as boundary_error:
        import logging
        logging.getLogger(__name__).error("Runtime boundary failed",
            exc_info=(RuntimeError, RuntimeError(type(boundary_error).__name__), boundary_error.__traceback__))
        return cfg
    scoped.multiplex_profiles = cfg.multiplex_profiles  # the verdict above, not a second unset flag
    return scoped
