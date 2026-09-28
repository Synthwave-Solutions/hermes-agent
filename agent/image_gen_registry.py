"""Image generation provider registry.

Populated by plugins at import-time via ``PluginContext.register_image_gen_provider()``;
the ``image_generate`` tool dispatches to :func:`get_active_provider`. Selection is
``image_gen.provider`` in config.yaml; when unset: the single *available* provider,
else ``fal`` if registered and available (legacy default), else ``None`` (the tool
points the user at ``hermes tools``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from agent.model_gateway import REFUSAL_CODE

from agent.image_gen_provider import ImageGenProvider
from agent.provider_registry import ProviderRegistry, configured_provider_name, is_available_safe

logger = logging.getLogger(__name__)


_registry: ProviderRegistry[ImageGenProvider] = ProviderRegistry(
    label="Image gen", provider_cls=ImageGenProvider, logger=logger,
)
_registry.export(globals())


def _resolve_active_provider() -> Optional[ImageGenProvider]:
    """Resolve the currently-active provider. Availability semantics (mirrors
    :mod:`agent.web_search_registry`): an explicitly configured provider is returned
    even if ``is_available()`` is False, so the dispatcher surfaces a precise
    "X_API_KEY is not set" error instead of silently switching backends; only the
    unconfigured fallback path is filtered by availability."""
    configured = configured_provider_name("image_gen", logger)
    snapshot = _registry.merged()
    if configured:
        if snapshot.get(configured) is not None:
            return snapshot[configured]
        logger.debug("image_gen.provider='%s' configured but not registered; falling back", configured)

    def _available(p: ImageGenProvider) -> bool:
        return is_available_safe(p, logger, "image_gen provider %s.is_available() raised %s")

    available = [p for p in snapshot.values() if _available(p)]
    if len(available) == 1:
        return available[0]
    fal = snapshot.get("fal")
    return fal if fal is not None and _available(fal) else None

class _GatewayRefusedProvider(ImageGenProvider):
    """Stands in for a provider that ``model_gateway.enforce`` refuses."""

    def __init__(self, name: str, refusal: Exception) -> None:
        self._name = name
        self._refusal = refusal

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return False

    def generate(self, prompt: str, aspect_ratio: str = "landscape", **kwargs: Any) -> Dict[str, Any]:
        from agent.image_gen_provider import error_response

        return error_response(
            error=str(self._refusal),
            error_type=REFUSAL_CODE,
            provider=self._name,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
        )

def _gateway_checked(provider: Optional[ImageGenProvider]) -> Optional[ImageGenProvider]:
    if provider is None or isinstance(provider, _GatewayRefusedProvider):
        return provider
    from agent import model_gateway

    refusal = model_gateway.registry_provider_refusal("image_gen", provider)
    if refusal is None:
        return provider
    return _GatewayRefusedProvider(provider.name, refusal)

def get_provider(name: str, *, scope=None) -> Optional[ImageGenProvider]:
    return _gateway_checked(_registry.get_provider(name, scope=scope))


def get_active_provider() -> Optional[ImageGenProvider]:
    return _gateway_checked(_resolve_active_provider())
