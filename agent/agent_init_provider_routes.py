"""Agent init provider routes.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from typing import Any, Optional

def _provider_default_routes(provider: str) -> set[str]:
    """Return known exact default routes for a canonical provider id."""
    from agent.agent_init import (
        normalize_route_base_url, suppress
    )
    routes: set[str] = set()

    def add(value):
        route = normalize_route_base_url(value)
        if route:
            routes.add(route)

    with suppress(Exception):
        from hermes_cli.providers import HERMES_OVERLAYS, get_provider
        overlay = HERMES_OVERLAYS.get(provider)
        provider_def = get_provider(provider, allow_network=False)
        add(getattr(overlay, "base_url_override", ""))
        add(getattr(provider_def, "base_url", ""))

    with suppress(Exception):
        from providers import get_provider_profile
        add(getattr(get_provider_profile(provider), "base_url", ""))

    with suppress(Exception):
        from hermes_cli.auth import PROVIDER_REGISTRY
        from hermes_cli.models import normalize_provider as normalize_model_provider
        from hermes_cli.providers import normalize_provider as normalize_registry_provider
        for provider_id, config in PROVIDER_REGISTRY.items():
            if normalize_registry_provider(normalize_model_provider(provider_id)) == provider:
                add(getattr(config, "inference_base_url", ""))

    if provider == "gemini":
        routes.update(f"{route.rstrip('/')}/openai" for route in list(routes))
    return routes


def _context_route_mismatch(
    configured_base_url: Any, active_base_url: Any, configured_provider: Any, active_provider: Any,
    *, already_normalized: bool = False,
) -> bool:
    """Return whether a context pin's configured route differs from runtime."""
    from agent.agent_init import (
        normalize_route_base_url, suppress
    )
    _norm = (lambda v: str(v or "")) if already_normalized else normalize_route_base_url
    configured_route, active_route = _norm(configured_base_url), _norm(active_base_url)
    if configured_route:
        return configured_route != active_route

    configured_provider = str(configured_provider or "").strip()
    active_provider = str(active_provider or "").strip()
    if not configured_provider:
        return False
    try:
        from hermes_cli.models import normalize_provider as normalize_model_provider
        configured_provider = normalize_model_provider(configured_provider)
        active_provider = normalize_model_provider(active_provider)
    except Exception:
        configured_provider = configured_provider.lower()
        active_provider = active_provider.lower()
    with suppress(Exception):
        from hermes_cli.providers import normalize_provider as normalize_registry_provider
        configured_provider = normalize_registry_provider(configured_provider)
        active_provider = normalize_registry_provider(active_provider)

    if active_route:
        configured_routes = _provider_default_routes(configured_provider)
        if configured_routes:
            return active_route not in configured_routes
        # Named/custom providers have no catalog default routes: an empty configured URL
        # with a matching provider identity is the same route (gateway display paths
        # compare the raw empty model.base_url and must not drop model.context_length).
        return not (active_provider and configured_provider == active_provider)
    return bool(
        configured_provider and active_provider and configured_provider != active_provider
    )


def _normalize_custom_provider_name(value: Any) -> str:
    """Mirror runtime normalization for a requested custom-provider identity."""
    return str(value or "").strip().lower().replace(" ", "-")


def _custom_provider_runtime_ids(value: Any) -> set[str]:
    """Return raw/menu identities that runtime accepts for a configured name."""

    normalized = _normalize_custom_provider_name(value)
    if not normalized:
        return set()
    return {normalized, f"custom:{normalized}"}


def _normalized_custom_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def _custom_provider_model_matches(agent_model: str, entry: dict[str, Any]) -> bool:

    agent_model_norm = str(agent_model or "").strip().lower()
    # Multi-model entries (`providers.<name>.models` mapping / legacy `models:` list):
    # matching ANY catalog entry counts, else a provider whose `model` differs from the
    # session model drops its extra_body (e.g. OpenAI service_tier) → wrong billing tier.
    models = entry.get("models")
    catalog = [str(m).strip().lower() for m in models] if isinstance(models, (dict, list, tuple)) else []
    if catalog and agent_model_norm in catalog:
        return True
    provider_model = str(entry.get("model", "") or "").strip().lower()
    return (not provider_model and not catalog) or provider_model == agent_model_norm


def _custom_provider_extra_body_for_agent(
    *, provider: str, model: str, base_url: str, custom_providers: list[dict[str, Any]]
) -> Optional[dict[str, Any]]:

    provider_norm = (provider or "").strip().lower()
    if provider_norm != "custom" and not provider_norm.startswith("custom:"):
        return None
    provider_key_filter = provider_norm.partition(":")[2].strip()
    target_url = _normalized_custom_base_url(base_url)
    if not target_url:
        return None

    fallback: Optional[dict[str, Any]] = None
    for entry in custom_providers or []:
        if not isinstance(entry, dict):
            continue
        entry_keys = {
            str(entry.get("provider_key", "") or "").strip().lower(),
            str(entry.get("name", "") or "").strip().lower(),
        }
        if provider_key_filter and provider_key_filter not in entry_keys:
            continue
        if _normalized_custom_base_url(entry.get("base_url")) != target_url:
            continue
        extra_body = entry.get("extra_body")
        if not isinstance(extra_body, dict) or not extra_body:
            continue
        if str(entry.get("model", "") or "").strip():
            if _custom_provider_model_matches(model, entry):
                return dict(extra_body)
        elif fallback is None:
            fallback = dict(extra_body)
    return fallback


def _merge_custom_provider_extra_body(agent, custom_providers: list[dict[str, Any]]) -> None:

    extra_body = _custom_provider_extra_body_for_agent(
        provider=agent.provider, model=agent.model, base_url=agent.base_url,
        custom_providers=custom_providers,
    )
    if not extra_body:
        return
    overrides = dict(getattr(agent, "request_overrides", {}) or {})
    merged_extra_body = dict(extra_body)
    existing_extra_body = overrides.get("extra_body")
    if isinstance(existing_extra_body, dict):
        merged_extra_body.update(existing_extra_body)
    overrides["extra_body"] = merged_extra_body
    agent.request_overrides = overrides
