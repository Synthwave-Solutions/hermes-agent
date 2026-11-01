"""Api server runtime options.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from typing import Any, Optional

def _coerce_request_bool(value: Any, default: bool = False) -> bool:
    """Normalize boolean-like payload values; only explicit bool-ish scalars count (some
    frontends send ``"false"`` for ``stream``, which is truthy), else ``default``."""
    from gateway.platforms.api_server import (
        _FALSE_REQUEST_BOOL_STRINGS, _TRUE_REQUEST_BOOL_STRINGS
    )
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_REQUEST_BOOL_STRINGS:
            return True
        return False if normalized in _FALSE_REQUEST_BOOL_STRINGS else default
    return bool(value) if isinstance(value, (int, float)) else default


def _clean_request_string(value: Any) -> Optional[str]:
    """Return a stripped request string, or None for absent/non-string values."""
    return (value.strip() or None) if isinstance(value, str) else None


def _apply_runtime_agent_overrides(
    runtime_kwargs: dict[str, Any], overrides: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Merge resolved provider/runtime fields into ``runtime_kwargs`` in place."""
    from gateway.platforms.api_server import (
        _RUNTIME_AGENT_OVERRIDE_KEYS
    )
    if not isinstance(overrides, dict):
        return runtime_kwargs
    for key in _RUNTIME_AGENT_OVERRIDE_KEYS:
        value = overrides.get(key)
        if value is None:
            continue
        runtime_kwargs[key] = list(value) if key == "args" and isinstance(value, (list, tuple)) else value
    return runtime_kwargs


def _resolve_request_runtime_agent_kwargs(provider: str, target_model: Optional[str] = None) -> dict[str, Any]:
    """gateway.run._resolve_runtime_agent_kwargs() for an explicit provider/model, so an API
    caller uses the same authenticated provider catalog without mutating config.yaml."""
    from hermes_cli.runtime_provider import resolve_runtime_provider, format_runtime_provider_error, _get_model_config
    try:
        runtime = resolve_runtime_provider(requested=provider, target_model=target_model)
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    return {
        **{k: runtime.get(k) for k in ("api_key", "base_url", "provider", "api_mode", "command")},
        "args": list(runtime.get("args") or []),
        "credential_pool": runtime.get("credential_pool")}


def _request_agent_overrides(
    body: Any, *, virtual_model: Optional[str] = None, allow_bare_model: bool = True
) -> dict[str, Any]:
    """Extract per-request model/provider/options for _run_agent.

    The virtual model (``hermes-agent``) means "gateway default". A bare ``model`` without
    ``provider`` is honored only when ``allow_bare_model`` (generic clients hardcode "gpt-4o";
    OpenAI-compatible handlers pass the ``direct_model_requests`` opt-in, Hermes-native
    endpoints always allow it). An explicit ``provider`` is always honored.
    """
    from gateway.platforms.api_server import (
        Any
    )
    if not isinstance(body, dict):
        return {}
    overrides: dict[str, Any] = {}
    provider = _clean_request_string(body.get("provider"))
    if provider:
        overrides["requested_provider"] = provider
    model = _clean_request_string(body.get("model"))
    if model and model != virtual_model and (provider or allow_bare_model):
        overrides["requested_model"] = model
    model_options = body.get("model_options")
    if isinstance(model_options, dict):
        overrides["model_options"] = dict(model_options)
    return overrides


def _request_relay_metadata(body: Any) -> dict[str, Any]:
    """Extract Relay metadata from an OpenAI request body."""
    if not isinstance(body, dict):
        return {}
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    return dict(metadata)
