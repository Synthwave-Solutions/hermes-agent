"""Auxiliary client gateway.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from typing import Any, Optional
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent.auxiliary_client import OpenAI

def _resolve_api_key_provider() -> tuple[Optional[OpenAI], Optional[str]]:
    """Try each API-key provider in PROVIDER_REGISTRY order; (client, model) or (None, None)."""
    from agent.auxiliary_client import (
        _apply_user_default_headers, _create_openai_client, _get_aux_model_for_provider, _is_provider_unhealthy, _maybe_wrap_anthropic, _model_gateway, _normalize_main_runtime, _pool_runtime_api_key, _pool_runtime_base_url, _profile_default_headers, _select_pool_entry, _to_openai_base_url, _try_anthropic, base_url_host_matches, build_nvidia_nim_headers, contextlib, logger
    )
    if _model_gateway.enforced():
        return _refuse_direct_lane("api-key")
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, resolve_api_key_provider_credentials
    except ImportError:
        logger.debug("Could not import PROVIDER_REGISTRY for API-key fallback")
        return None, None
    for provider_id, pconfig in PROVIDER_REGISTRY.items():
        if pconfig.auth_type != "api_key":
            continue
        if _is_provider_unhealthy(provider_id):
            logger.debug("Auxiliary api-key chain: %s is unhealthy, skipping", provider_id)
            continue
        if provider_id in {"anthropic", "copilot"}:
            # Ambient Claude Code/gh credentials require explicit auxiliary opt-in.
            with contextlib.suppress(ImportError):
                from hermes_cli.auth import is_provider_explicitly_configured
                if not is_provider_explicitly_configured(provider_id):
                    continue
        if provider_id == "anthropic":
            return _try_anthropic()
        model = _get_aux_model_for_provider(provider_id) or None
        if model is None:
            continue  # skip provider if we don't know a valid aux model
        pool_present, entry = _select_pool_entry(provider_id)
        if pool_present:
            api_key = _pool_runtime_api_key(entry)
            if not api_key:
                continue
            raw_base_url = _pool_runtime_base_url(entry, pconfig.inference_base_url) or pconfig.inference_base_url
            via = " via pool"
        else:
            creds = resolve_api_key_provider_credentials(provider_id)
            api_key = str(creds.get("api_key", "")).strip()
            if not api_key:
                continue
            raw_base_url = str(creds.get("base_url", "")).strip().rstrip("/") or pconfig.inference_base_url
            via = ""
        # The session's own endpoint wins for its provider: the key was issued for that gateway, and
        # sending it to the registry default 401s, then quarantines the provider the main model is on.
        runtime = _normalize_main_runtime(None)
        if runtime.get("provider") == provider_id and runtime.get("base_url"):
            raw_base_url = runtime["base_url"].rstrip("/")
            if isinstance(runtime.get("api_key"), str) and runtime["api_key"]:
                api_key = runtime["api_key"]
            via = " (session endpoint)"
        logger.debug("Auxiliary text client: %s (%s)%s", pconfig.name, model, via)
        # Native Gemini, else OpenAI-wire + Anthropic rewrap.
        base_url = _to_openai_base_url(raw_base_url)
        if provider_id == "gemini":
            from agent.gemini_native_adapter import GeminiNativeClient, is_native_gemini_base_url
            if is_native_gemini_base_url(base_url):
                return GeminiNativeClient(api_key=api_key, base_url=base_url), model
        if base_url_host_matches(base_url, "api.kimi.com"):
            headers = {"User-Agent": "claude-code/0.1.0"}
        elif base_url_host_matches(base_url, "githubcopilot.com"):
            from hermes_cli.models import copilot_default_headers
            headers = copilot_default_headers()
        elif base_url_host_matches(base_url, "integrate.api.nvidia.com"):
            headers = build_nvidia_nim_headers(base_url)
        else:
            headers = _profile_default_headers(provider_id)
        extra = {"default_headers": headers} if headers else {}
        merged = _apply_user_default_headers(extra.get("default_headers"))
        if merged:
            extra["default_headers"] = merged
        client = _create_openai_client(api_key=api_key, base_url=base_url, **extra)
        return _maybe_wrap_anthropic(client, model, api_key, raw_base_url), model
    return None, None


def _refuse_direct_lane(label: str) -> tuple[None, None]:
    """A direct provider lane under ``model_gateway.enforce``: never built."""
    from agent.auxiliary_client import (
        logger
    )
    logger.debug("Auxiliary: %s lane refused under model_gateway.enforce", label)
    return None, None


def _try_model_gateway() -> tuple[Optional[Any], Optional[str]]:
    """The gateway lane of the step 3 chain (only lane under enforcement)."""
    return _resolve_gateway_client("auto", None)


def _resolve_gateway_client(
    provider: Optional[str],
    model: Optional[str],
    *,
    async_mode: bool = False,
    explicit_base_url: Optional[str] = None,
    explicit_api_key: Optional[str] = None,
    main_runtime: Optional[dict[str, Any]] = None,
    is_vision: bool = False,
) -> tuple[Optional[Any], Optional[str]]:
    """Build the gateway client that replaces every provider under enforcement.

    The model is the caller's (a task or fallback entry) or the main model;
    the key is the caller's own gateway key, never a provider key. A gateway
    marked unhealthy raises ``ModelGatewayUnavailable`` instead of choosing
    another lane.
    """
    from agent.auxiliary_client import (
        _aux_probe_active, _create_openai_client, _is_provider_unhealthy, _model_gateway, _normalize_main_runtime, _read_main_api_key, _read_main_base_url, _read_main_model, _read_main_model_for_aux, _resolve_moa_aggregator, _to_async_client, logger
    )
    if _is_provider_unhealthy(_model_gateway.GATEWAY_LABEL):
        raise _model_gateway.ModelGatewayUnavailable(
            "the model gateway is marked unhealthy after a failed call; "
            "direct provider lanes are disabled by model_gateway.enforce"
        )
    runtime = _normalize_main_runtime(main_runtime)
    if (provider or "").strip().lower() == "moa" and model:
        # A MoA preset name is not a wire model; its acting model is the aggregator.
        _agg_provider, _agg_model = _resolve_moa_aggregator(model)
        model = _agg_model or model
    base_url = _model_gateway.gateway_base_url()
    fallback_key = ""
    if explicit_base_url and _model_gateway.is_gateway_url(explicit_base_url):
        fallback_key = explicit_api_key or ""
    runtime_key = runtime.get("api_key")
    if (
        not fallback_key
        and isinstance(runtime_key, str)
        and _model_gateway.is_gateway_url(str(runtime.get("base_url") or ""))
    ):
        fallback_key = runtime_key
    if not fallback_key and _model_gateway.is_gateway_url(_read_main_base_url()):
        main_key = _read_main_api_key()
        fallback_key = main_key if isinstance(main_key, str) else ""
    try:
        base_url, api_key = _model_gateway.require_gateway(fallback_key=fallback_key)
    except _model_gateway.ModelGatewayUnavailable:
        if _aux_probe_active():
            return None, None
        raise
    final_model = (
        model
        or str(runtime.get("model") or "")
        or _read_main_model_for_aux()
        or _read_main_model()
    )
    if not final_model:
        logger.warning(
            "Auxiliary: no model for the gateway lane (provider=%s); set "
            "auxiliary.<task>.model or model.default", provider or "auto",
        )
        return None, None
    client = _create_openai_client(api_key=api_key, base_url=base_url)
    client._hermes_aux_effective_provider = _model_gateway.GATEWAY_LABEL
    if async_mode:
        client, final_model = _to_async_client(client, final_model, is_vision=is_vision)
        client._hermes_aux_effective_provider = _model_gateway.GATEWAY_LABEL
    return client, final_model
