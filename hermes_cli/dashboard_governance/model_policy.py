from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import AccessDecision, EffectiveAccess


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _norm_provider(value: Any) -> str:
    return _norm(value).lower()


def _allowed_by(values: frozenset[str], value: str) -> bool:
    return "*" in values or value in values


def decide_model_access(
    access: EffectiveAccess | None,
    *,
    provider: str,
    model: str,
) -> AccessDecision:
    if access is None or access.mode != "enforce":
        return AccessDecision(True, "governance_inactive")

    grants = access.grants
    provider_name = _norm_provider(provider)
    model_name = _norm(model)
    if not provider_name:
        return AccessDecision(False, "model_provider_required")
    if not _allowed_by(grants.model_providers, provider_name):
        return AccessDecision(False, "model_provider_not_allowed")
    if model_name and not _allowed_by(grants.models, model_name):
        return AccessDecision(False, "model_not_allowed")
    if not model_name and "*" not in grants.models:
        return AccessDecision(False, "model_required")
    return AccessDecision(True, "allowed")


def filter_model_options_payload(payload: dict[str, Any], access: EffectiveAccess | None) -> dict[str, Any]:
    if access is None or access.mode != "enforce":
        return payload

    providers = payload.get("providers")
    if not isinstance(providers, list):
        return payload

    filtered_providers: list[dict[str, Any]] = []
    for provider_row in providers:
        if not isinstance(provider_row, dict):
            continue
        slug = _norm_provider(provider_row.get("slug") or provider_row.get("provider"))
        if not slug or not _allowed_by(access.grants.model_providers, slug):
            continue
        row = deepcopy(provider_row)
        models = row.get("models")
        if isinstance(models, list) and "*" not in access.grants.models:
            row["models"] = [model for model in models if _allowed_by(access.grants.models, _norm(model))]
            if not row["models"]:
                continue
        filtered_providers.append(row)

    filtered = dict(payload)
    filtered["providers"] = filtered_providers
    return filtered
