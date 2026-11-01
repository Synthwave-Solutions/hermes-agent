"""Turn recovery diagnostics.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from typing import Any, Optional

def log_api_error_attempt(
    agent: Any, api_error: Exception, *, retry_count: int, max_retries: int,
    status_code: Optional[int], elapsed_time: float, api_messages: Any, approx_tokens: int,
    retryable: bool = True,
) -> tuple[str, str, Any, Any, Any]:
    """Log one failed API attempt (warning + buffered retry trace, OpenRouter "no tool
    endpoints" hint, bare-404 missing-vendor-prefix hint); the buffer only surfaces if every
    retry+fallback exhausts. Returns ``(error_type, error_msg, provider, base_url, model)``.

    ``retryable=False`` (the classifier's verdict, e.g. a 401 on a static-key route) is
    named on the line: a bare ``attempt 1/3`` promises a second attempt that never comes
    and sends readers hunting for a retry bug (#73237)."""
    from agent.turn_recovery import (
        _blines, _missing_vendor_prefix_suggestion, logger
    )
    error_type = type(api_error).__name__
    error_msg = str(api_error).lower()
    _error_summary = agent._summarize_api_error(api_error)
    _attempt = f"attempt {retry_count}/{max_retries}" + ("" if retryable else ", not retryable")
    logger.warning(
        "API call failed (%s) error_type=%s %s summary=%s",
        _attempt, error_type, agent._client_log_context(), _error_summary,
    )

    _provider = getattr(agent, "provider", "unknown")
    _base = getattr(agent, "base_url", "unknown")
    _model = getattr(agent, "model", "unknown")
    _blines(agent, f"⚠️  {_attempt[0].upper()}{_attempt[1:]} failed: {_error_summary}")
    # Exception class, endpoint, raw body and token counts are developer detail: verbose only.
    if getattr(agent, "verbose_logging", False):
        _status_code_str = f" [HTTP {status_code}]" if status_code else ""
        _blines(
            agent,
            f"   🔌 {error_type}{_status_code_str}  Provider: {_provider}  Model: {_model}",
            f"   🌐 Endpoint: {_base}",
        )
        if status_code and status_code < 500:
            _err_body = getattr(api_error, "body", None)
            _err_body_str = str(_err_body)[:300] if _err_body else None
            if _err_body_str:
                _blines(agent, f"   📋 Details: {_err_body_str}")
        _blines(agent, f"   ⏱️  Elapsed: {elapsed_time:.2f}s  Context: {len(api_messages)} msgs, ~{approx_tokens:,} tokens")

    if agent._is_openrouter_url() and "support tool use" in error_msg:
        _blines(agent, f"   💡 No OpenRouter providers for {_model} support tool calling with your current settings.")
        from agent.chat_completion_helpers import _provider_preferences_for_agent
        if _provider_preferences_for_agent(agent).get("only"):
            _blines(
                agent,
                "      Your provider_routing.only restriction is filtering out tool-capable providers.",
                "      Try removing the restriction or adding providers that support tools for this model.",
            )
        _blines(agent, f"      Check which providers support tools: https://openrouter.ai/models/{_model}")

    # Bare 404 on a ``vendor/model`` catalogue usually means the id lost its prefix; the
    # provider never names the model, so we do.
    _suggestion = _missing_vendor_prefix_suggestion(api_error, _provider, _model)
    if _suggestion:
        _blines(
            agent,
            f"   💡 Model '{_model}' is not a valid id for provider {_provider} — it is missing its vendor prefix.",
            f"      Did you mean '{_suggestion}'?  Re-pick it with /model.",
        )
    return error_type, error_msg, _provider, _base, _model


def describe_invalid_response(agent: Any, response: Any, api_duration: float) -> tuple[str, str, str]:
    """Diagnostics for an empty/malformed response: ``(error_msg, provider_name,
    failure_hint)``. The hint is derived from the provider error code (524/504/429/
    5xx) and the response time, instead of always assuming rate limiting."""
    from agent.turn_recovery import _codex_soft_failure_error, _failure_hint_for
    import logging
    error_msg = "Unknown"
    provider_name = "Unknown"
    _has_error = bool(response and hasattr(response, 'error') and response.error)
    if _has_error:
        # A typed ``ResponseError`` stringifies as its repr; show the provider's message.
        error_msg = _codex_soft_failure_error(response).get("message") or str(response.error)
        if hasattr(response.error, 'metadata') and response.error.metadata:
            provider_name = response.error.metadata.get('provider_name', 'Unknown')
    elif response and hasattr(response, 'message') and response.message:
        error_msg = str(response.message)

    # OpenRouter often returns the actual model used.
    if provider_name == "Unknown" and response and hasattr(response, 'model') and response.model:
        provider_name = f"model={response.model}"

    if provider_name == "Unknown" and response:
        resp_attrs = {k: str(v)[:100] for k, v in vars(response).items() if not k.startswith('_')}
        if agent.verbose_logging:
            logging.debug(f"Response attributes for invalid response: {resp_attrs}")

    _resp_error_code = None
    if _has_error:
        _code_raw = getattr(response.error, 'code', None)
        if _code_raw is None and isinstance(response.error, dict):
            _code_raw = response.error.get('code')
        if _code_raw is not None:
            try:
                _resp_error_code = int(_code_raw)
            except (TypeError, ValueError):
                pass

    return error_msg, provider_name, _failure_hint_for(_resp_error_code, api_duration)
