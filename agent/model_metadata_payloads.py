"""Model metadata payloads.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from typing import Any, Optional

def _iter_nested_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_nested_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_nested_dicts(item)


def _coerce_reasonable_int(value: Any, minimum: int = 1024, maximum: int = 10_000_000) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        result = int(value.strip().replace(",", "") if isinstance(value, str) else value)
    except (TypeError, ValueError):
        return None
    return result if minimum <= result <= maximum else None


def _extract_first_int(payload: dict[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    keyset = {key.lower() for key in keys}
    for mapping in _iter_nested_dicts(payload):
        for key, value in mapping.items():
            if str(key).lower() in keyset:
                coerced = _coerce_reasonable_int(value)
                if coerced is not None:
                    return coerced
    return None


def _extract_flat_context_length(payload: dict[str, Any]) -> Optional[int]:
    """Top-level-only context WINDOW read (no nested walk, so an unrelated nested section can't leak
    a same-named key). ``max_tokens`` is NOT a window key: on OpenAI-compatible passthroughs it is the max OUTPUT."""
    from agent.model_metadata import (
        _CONTEXT_LENGTH_KEYS
    )
    return next((c for c in map(_coerce_reasonable_int, (payload.get(k) for k in _CONTEXT_LENGTH_KEYS)) if c is not None), None)


def _context_length_from_model_payload(payload: dict[str, Any]) -> Optional[int]:
    """Context window from a ``/v1/models`` object: window keys first, ``max_tokens`` last (Anthropic
    payloads carry ``max_input_tokens`` = 1M window AND ``max_tokens`` = 128k OUTPUT cap)."""
    if not isinstance(payload, dict):
        return None
    ctx = _extract_flat_context_length(payload)
    if ctx is not None:
        return ctx
    raw = payload.get("max_tokens")
    return int(raw) if isinstance(raw, (int, float)) and int(raw) > 0 else None


def _normalize_token_rates(pricing: dict[str, Any], unit: Any) -> dict[str, Any]:
    """Rescale the generic path's token rates to per-token strings (the contract usage_pricing
    multiplies by 1e6), the way the Novita/DeepInfra branches already do for their known units."""
    from agent.model_metadata import (
        _PER_MILLION_QUOTE_MIN, _PRICING_UNIT_DIVISORS, _TOKEN_RATE_FIELDS
    )
    rates: dict[str, float] = {}
    for key in _TOKEN_RATE_FIELDS:
        try:
            rates[key] = float(pricing[key])
        except (KeyError, TypeError, ValueError):
            continue
    divisor = _PRICING_UNIT_DIVISORS.get(str(unit or "").strip().lower())
    if divisor is None:
        divisor = 1_000_000 if any(v >= _PER_MILLION_QUOTE_MIN for v in rates.values()) else 1
    if divisor != 1:
        pricing.update({key: str(value / divisor) for key, value in rates.items()})
    return pricing


def _extract_pricing(payload: dict[str, Any]) -> dict[str, Any]:
    from agent.model_metadata import (
        Any
    )
    def _per_token(source: dict[str, Any], fields: dict[str, str], scale) -> dict[str, Any]:
        # Provider $/MTok (or Novita's 1/10_000-$ per M) -> per-token strings, the same path usage_pricing uses for OpenRouter.
        return {target: str(scale(float(source[key]))) for target, key in fields.items() if source.get(key) is not None}
    novita_fields = {"prompt": "input_token_price_per_m", "completion": "output_token_price_per_m"}
    if any(payload.get(k) is not None for k in novita_fields.values()):
        return _per_token(payload, novita_fields, lambda v: v / 10_000 / 1_000_000)
    # DeepInfra ships pricing under ``metadata.pricing`` in $/MTok.
    metadata = payload.get("metadata")
    deepinfra_pricing = metadata.get("pricing") if isinstance(metadata, dict) else None
    deepinfra_fields = {"prompt": "input_tokens", "completion": "output_tokens", "cache_read": "cache_read_tokens"}
    if isinstance(deepinfra_pricing, dict) and any(k in deepinfra_pricing for k in deepinfra_fields.values()):
        return _per_token(deepinfra_pricing, deepinfra_fields, lambda v: v / 1_000_000)
    alias_map = {
        "prompt": ("prompt", "input", "input_cost_per_token", "prompt_token_cost"),
        "completion": ("completion", "output", "output_cost_per_token", "completion_token_cost"),
        "request": ("request", "request_cost"),
        "cache_read": ("cache_read", "cached_prompt", "input_cache_read", "cache_read_cost_per_token"),
        "cache_write": ("cache_write", "cache_creation", "input_cache_write", "cache_write_cost_per_token"),
    }
    for mapping in _iter_nested_dicts(payload):
        normalized = {str(key).lower(): value for key, value in mapping.items()}
        pricing: dict[str, Any] = {}
        for target, aliases in alias_map.items():
            for alias in aliases:
                if alias in normalized and normalized[alias] not in {None, ""}:
                    pricing[target] = normalized[alias]
                    break
        if pricing:
            return _normalize_token_rates(pricing, normalized.get("unit"))
    return {}


def _add_model_aliases(cache: dict[str, dict[str, Any]], model_id: str, entry: dict[str, Any]) -> None:
    cache[model_id] = entry
    if "/" in model_id:
        cache.setdefault(model_id.split("/", 1)[1], entry)


def _endpoint_model_entry(model: dict[str, Any], model_id: str, context_length: Optional[int]) -> dict[str, Any]:
    """Cache entry for one ``/models`` item; optional keys are set only when known."""
    from agent.model_metadata import (
        _MAX_COMPLETION_KEYS
    )
    optional = (("context_length", context_length), ("max_completion_tokens", _extract_first_int(model, _MAX_COMPLETION_KEYS)), ("pricing", _extract_pricing(model) or None))
    return {"name": model.get("name", model_id), **{k: v for k, v in optional if v is not None}}


def _lmstudio_loaded_context(model: dict[str, Any]) -> Optional[int]:
    """Context of the first loaded LM Studio instance (the runtime value), else None."""
    for inst in model.get("loaded_instances", []) or []:
        cfg = inst.get("config", {}) if isinstance(inst, dict) else None
        ctx = cfg.get("context_length") if isinstance(cfg, dict) else None
        if isinstance(ctx, int) and ctx > 0:
            return ctx
    return None


def _parse_models_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    from agent.model_metadata import (
        Any, _CONTEXT_LENGTH_KEYS
    )
    cache: dict[str, dict[str, Any]] = {}
    for model in payload.get("data", []):
        model_id = model.get("id") if isinstance(model, dict) else None
        if model_id:
            _add_model_aliases(cache, model_id, _endpoint_model_entry(model, model_id, _extract_first_int(model, _CONTEXT_LENGTH_KEYS)))
    return cache
