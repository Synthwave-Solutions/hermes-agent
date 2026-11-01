"""Chat completion gateway.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

def _extract_omniroute_route(http_response) -> tuple[str, str] | None:
    """Extract the provider and model routed by OmniRoute response headers."""
    headers = getattr(http_response, "headers", None)
    if headers is None:
        return None
    try:
        model = headers.get("x-omniroute-model")
        provider = headers.get("x-omniroute-provider")
    except (AttributeError, TypeError):
        return None
    if model and provider:
        return provider, model
    return None


def capture_omniroute_route(agent, response) -> None:
    """Update context compression for the model actually selected by OmniRoute."""
    from agent.chat_completion_helpers import (
        logger
    )
    route_info = getattr(agent, "_omniroute_route_info", None)
    agent._omniroute_route_info = None
    if route_info:
        provider, model = route_info
        resolved_model = f"{provider}/{model}"
    else:
        resolved_model = getattr(response, "model", None) if response else None
    if not resolved_model or resolved_model == agent.model:
        return
    # OmniRoute's response header uses the short provider id "cx" for
    # requests addressed to "codex". This is the same model, not a route
    # change. Preserve the compressor's already-resolved context window;
    # probing the unrecognized alias here blocks terminal delivery on HTTP
    # metadata timeouts after the answer has already streamed to the user.
    def canonical_route(model):
        if isinstance(model, str) and model.startswith("cx/"):
            return "codex/" + model[3:]
        return model

    compressor = getattr(agent, "context_compressor", None)
    current_model = getattr(compressor, "model", None)
    if canonical_route(resolved_model) == canonical_route(current_model):
        return
    if getattr(agent, "_effective_routed_model", None) == resolved_model:
        return
    from agent.model_metadata import get_model_context_length
    try:
        context_length = get_model_context_length(
            resolved_model, base_url=agent.base_url, api_key=agent.api_key,
            provider=agent.provider,
        )
    except Exception as boundary_error:
        import logging
        logging.getLogger(__name__).error("Runtime boundary failed",
            exc_info=(RuntimeError, RuntimeError(type(boundary_error).__name__), boundary_error.__traceback__))
        return
    if context_length is None:
        logger.debug("No context length resolved for routed model %r", resolved_model)
        return
    agent._effective_routed_model = resolved_model
    agent._effective_context_length = context_length
    agent.context_compressor.update_model(
        model=resolved_model, context_length=context_length,
        base_url=agent.base_url or "", api_key=agent.api_key or "",
        provider=agent.provider or "", api_mode=agent.api_mode or "",
        max_tokens=getattr(agent, "max_tokens", None),
    )


def _agent_request_base_url(agent) -> str:
    if getattr(agent, "api_mode", "") == "anthropic_messages":
        return str(getattr(agent, "_anthropic_base_url", None) or getattr(agent, "base_url", "") or "")
    return str(getattr(agent, "base_url", "") or "")


def _check_model_gateway_route(agent) -> None:
    """Refuse a main-model request that bypasses the model gateway (R2).

    Under ``model_gateway.enforce`` every completion path (chat, Responses,
    Messages) must target the gateway; Bedrock's native Converse API never
    does. MoA's facade is exempt because each of its slots is an auxiliary
    call that resolves to the gateway itself.
    """
    from agent import model_gateway

    if not model_gateway.enforced():
        return
    if (getattr(agent, "provider", "") or "").strip().lower() == "moa":
        return
    if getattr(agent, "api_mode", "") == "bedrock_converse" or not model_gateway.is_gateway_url(
        _agent_request_base_url(agent)
    ):
        raise model_gateway.ModelGatewayRefused(
            "model",
            getattr(agent, "provider", "") or "unknown",
            "the agent's endpoint is not the model gateway",
        )


def _apply_model_gateway_identity(agent, kwargs: dict) -> dict:
    """The ``user`` field and session header on every gateway completion."""
    from agent import model_gateway

    return model_gateway.apply_request_identity(
        kwargs,
        base_url=_agent_request_base_url(agent),
        api_mode=str(getattr(agent, "api_mode", "") or "chat_completions"),
        session_id=getattr(agent, "session_id", None),
    )
