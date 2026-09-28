"""Video Generation Provider Registry.

Populated by plugins via ``PluginContext.register_video_gen_provider()``;
consumed by the ``video_generate`` tool. The active provider is
``video_gen.provider`` from ``config.yaml``; a configured-but-unregistered name
fails closed. If unset, the single *available* registered provider is used
(mirrors ``agent/image_gen_registry.py`` minus its legacy ``fal`` preference)
so a box with credentials for only one backend auto-selects it; otherwise None
and the tool points the user at ``hermes tools``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from agent.model_gateway import REFUSAL_CODE

from agent.provider_registry import ProviderRegistry, configured_provider_name, is_available_safe
from agent.video_gen_provider import VideoGenProvider

logger = logging.getLogger(__name__)


_registry: ProviderRegistry[VideoGenProvider] = ProviderRegistry(
    label="Video gen", provider_cls=VideoGenProvider, logger=logger,
)
_registry.export(globals())

def _resolve_active_provider() -> Optional[VideoGenProvider]:
    """Resolve the currently-active provider (see module docstring)."""
    configured = configured_provider_name("video_gen", logger)
    snapshot = _registry.merged()

    if configured:
        provider = snapshot.get(configured)
        if provider is None:
            logger.debug(
                "video_gen.provider='%s' configured but not registered; failing closed", configured
            )
        return provider

    available = [
        p for p in snapshot.values()
        if is_available_safe(p, logger, "video_gen provider %s.is_available() raised %s")
    ]
    return available[0] if len(available) == 1 else None

class _GatewayRefusedProvider(VideoGenProvider):
    """Stands in for a provider that ``model_gateway.enforce`` refuses."""

    def __init__(self, name: str, refusal: Exception) -> None:
        self._name = name
        self._refusal = refusal

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return False

    def generate(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        from agent.video_gen_provider import error_response

        return error_response(
            error=str(self._refusal),
            error_type=REFUSAL_CODE,
            provider=self._name,
            prompt=prompt,
        )

def _gateway_checked(provider: Optional[VideoGenProvider]) -> Optional[VideoGenProvider]:
    if provider is None or isinstance(provider, _GatewayRefusedProvider):
        return provider
    from agent import model_gateway

    refusal = model_gateway.registry_provider_refusal("video_gen", provider)
    if refusal is None:
        return provider
    return _GatewayRefusedProvider(provider.name, refusal)

def get_provider(name: str, *, scope=None) -> Optional[VideoGenProvider]:
    return _gateway_checked(_registry.get_provider(name, scope=scope))


def get_active_provider() -> Optional[VideoGenProvider]:
    return _gateway_checked(_resolve_active_provider())
