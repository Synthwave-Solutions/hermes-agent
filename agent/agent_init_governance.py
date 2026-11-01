"""Agent init governance.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

def _enforce_dashboard_governance_model_policy(agent) -> None:
    import logging
    try:
        from hermes_cli.dashboard_governance.context import current_governance_context
        from hermes_cli.dashboard_governance.model_policy import model_allowed_for_context

        ctx = current_governance_context()
        if ctx is None:
            return
        decision = model_allowed_for_context(ctx, provider=agent.provider, model=agent.model)
    except Exception as exc:
        logging.getLogger(__name__).error("_enforce_dashboard_governance_model_policy: operation failed",
            exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
        try:
            ctx = current_governance_context()  # type: ignore[name-defined]
            if ctx is not None and (getattr(ctx.access, "mode", "off") == "enforce" or getattr(ctx, "continuation_contexts", ()) or getattr(ctx, "workspace_path", "")):
                raise PermissionError("dashboard governance denied model: governance_model_policy_error") from exc
        except NameError:
            pass
        return
    if not decision.allowed:
        raise PermissionError(f"dashboard governance denied model: {decision.reason}")
