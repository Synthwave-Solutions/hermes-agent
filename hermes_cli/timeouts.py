from __future__ import annotations


def provider_timeout_id(agent) -> str:
    """Retain the named router identity after compatible-client resolution."""
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    requested = str(getattr(agent, "requested_provider", "") or "").strip().lower()
    if provider in {"custom", "openai"} and requested == "custom:omniroute":
        return requested
    return provider


def resolve_agent_provider_timeout(agent, timeout_getter) -> float | None:
    """Prefer named route settings while retaining explicit transport defaults.

    Resolve each timeout field separately: a route may override only request or
    stale patience. The configured canonical provider/model value still applies
    when that named lookup supplies no valid value.
    """
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    timeout_provider = provider_timeout_id(agent)
    model = getattr(agent, "model", None)
    value = timeout_getter(timeout_provider, model)
    if value is None and timeout_provider != provider:
        return timeout_getter(provider, model)
    return value


def uses_local_inference_patience(agent, base_url: str | None = None) -> bool:
    """Keep native prefill patience separate from a named remote router.

    OmniRoute's loopback listener forwards remote inference. Its explicit
    provider identity must not disable the ordinary provider watchdog just
    because the router is reached on localhost. Unknown custom endpoints keep
    their existing native behavior; a fallback's current provider wins over a
    retained requested-provider label.
    """
    if provider_timeout_id(agent) == "custom:omniroute":
        return False
    from agent.model_metadata import is_local_endpoint

    url = base_url if base_url is not None else getattr(agent, "base_url", "")
    return bool(url and is_local_endpoint(url))


def _provider_timeout_config(providers, provider_id):
    if not isinstance(providers, dict):
        return {}
    entry = providers.get(provider_id)
    if entry is None and provider_id == "custom:omniroute":
        # Runtime names this entry custom:omniroute, while its saved provider
        # key is normally omniroute. An explicit exact key takes precedence.
        entry = providers.get("omniroute", {})
    return entry


def _coerce_timeout(raw: object) -> float | None:
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None
    if timeout <= 0:
        return None
    return timeout


def get_provider_request_timeout(
    provider_id: str, model: str | None = None
) -> float | None:
    """Return a configured provider request timeout in seconds, if any."""
    if not provider_id:
        return None

    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except Exception:
        return None

    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    provider_config = _provider_timeout_config(providers, provider_id)
    if not isinstance(provider_config, dict):
        return None

    model_config = _get_model_config(provider_config, model)
    if model_config is not None:
        timeout = _coerce_timeout(model_config.get("timeout_seconds"))
        if timeout is not None:
            return timeout

    return _coerce_timeout(provider_config.get("request_timeout_seconds"))


def get_provider_stale_timeout(
    provider_id: str, model: str | None = None
) -> float | None:
    """Return a configured non-stream stale timeout in seconds, if any."""
    if not provider_id:
        return None

    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except Exception:
        return None

    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    provider_config = _provider_timeout_config(providers, provider_id)
    if not isinstance(provider_config, dict):
        return None

    model_config = _get_model_config(provider_config, model)
    if model_config is not None:
        timeout = _coerce_timeout(model_config.get("stale_timeout_seconds"))
        if timeout is not None:
            return timeout

    return _coerce_timeout(provider_config.get("stale_timeout_seconds"))


def _get_model_config(
    provider_config: dict[str, object], model: str | None
) -> dict[str, object] | None:
    if not model:
        return None

    models = provider_config.get("models", {})
    model_config = models.get(model, {}) if isinstance(models, dict) else {}
    if isinstance(model_config, dict):
        return model_config
    return None
