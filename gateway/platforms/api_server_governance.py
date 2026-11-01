"""Api server governance.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

def _beleidspad() -> str:
    """Het beleid van de machine zelf, los van HOME of HERMES_HOME."""
    from gateway.platforms.api_server import (
        os
    )
    try:
        import pwd

        thuis = pwd.getpwuid(os.getuid()).pw_dir if hasattr(os, "getuid") else os.path.expanduser("~")
    except (ImportError, OSError, KeyError):
        thuis = os.path.expanduser("~")
    return os.path.join(thuis, ".hermes", "dashboard-governance.yaml")


def _caller_email_from_request(request) -> str:
    from gateway.platforms.api_server import (
        _CALLER_PREFIX
    )
    rauw = (request.headers.get("X-Synthpulse-Actor") or "").strip().lower()
    if rauw:
        return rauw
    sleutel = (request.headers.get("X-Hermes-Session-Key") or "").strip().lower()
    if sleutel.startswith(_CALLER_PREFIX):
        return sleutel[len(_CALLER_PREFIX):].strip()
    return ""


def _bind_caller_governance(request):
    from gateway.platforms.api_server import (
        logger
    )
    from hermes_cli.synthpulse_settings import enabled
    if enabled("api_server.disable_caller_governance", False):
        return None
    email = _caller_email_from_request(request)
    if not email or "@" not in email:
        return None
    try:
        from hermes_cli.dashboard_governance import (
            GovernanceSubject,
            load_governance_policy,
            resolve_effective_access,
        )
        from hermes_cli.dashboard_governance.context import (
            DashboardGovernanceContext,
            bind_governance_context,
        )

        onderwerp = GovernanceSubject(email=email, groups=[])
        # Het beleidspad expliciet meegeven. Onder de multiplexer wijst
        # get_hermes_home() naar de profielmap van de beller, en daar staat
        # geen beleid; dan valt de loader stil terug op "governance uit" en
        # doet de hele laag niets. Dezelfde val als bij de folderguard.
        toegang = resolve_effective_access(
            load_governance_policy(path=_beleidspad()), onderwerp)
        return bind_governance_context(
            DashboardGovernanceContext(subject=onderwerp, access=toegang))
    except Exception as boundary_error:
        import logging
        logging.getLogger(__name__).error("Runtime boundary failed",
            exc_info=(RuntimeError, RuntimeError(type(boundary_error).__name__), boundary_error.__traceback__))
        return None


def _release_caller_governance(token) -> None:
    import logging
    if token is None:
        return
    try:
        from hermes_cli.dashboard_governance.context import reset_governance_context

        reset_governance_context(token)
    except (ImportError, ValueError):
        logging.getLogger(__name__).debug("_release_caller_governance: optional fallback used")
