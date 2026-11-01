"""Model metadata endpoint.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from typing import Any

def _lmstudio_native_models(normalized: str, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    """LM Studio ``/api/v1/models`` → cache; context comes from the first loaded instance."""
    from agent.model_metadata import (
        Any, _lmstudio_server_root, _model_metadata_payloads, model_metadata_http
    )
    response = model_metadata_http.get(_lmstudio_server_root(normalized).rstrip("/") + "/api/v1/models", headers=headers, timeout=(5, 10), verify=model_metadata_http.resolve_verify(normalized))
    response.raise_for_status()
    cache: dict[str, dict[str, Any]] = {}
    for model in response.json().get("models", []):
        model_id = (model.get("key") or model.get("id")) if isinstance(model, dict) else None
        if not model_id:
            continue
        entry = _model_metadata_payloads._endpoint_model_entry(model, model_id, _model_metadata_payloads._lmstudio_loaded_context(model))
        _model_metadata_payloads._add_model_aliases(cache, model_id, entry)
        alt_id = model.get("id")
        if isinstance(alt_id, str) and alt_id and alt_id != model_id:
            _model_metadata_payloads._add_model_aliases(cache, alt_id, entry)
    return cache


def _apply_llamacpp_props(cache: dict[str, dict[str, Any]], request_candidate: str, headers: dict[str, str], verify) -> None:
    """Overwrite ``context_length`` with llama.cpp's allocated ``n_ctx`` from /props (``/v1/props``, then
    ``/props`` for older builds). In router mode the bare endpoint 400s, so each LOADED child is read
    via ``/props?model=``; unloaded children are skipped — probing could autoload them."""
    from agent.model_metadata import (
        Any, model_metadata_http
    )
    base = request_candidate.rstrip("/").replace("/v1", "")
    def _props(params=None):
        resp = model_metadata_http.get(base + "/v1/props", params=params, headers=headers, timeout=5, verify=verify)
        if resp.is_error:
            resp = model_metadata_http.get(base + "/props", params=params, headers=headers, timeout=5, verify=verify)
        return resp
    def _n_ctx(props: dict[str, Any]) -> Any:
        return (props.get("default_generation_settings") or {}).get("n_ctx")
    props_resp = _props()
    if not props_resp.is_error:
        props = props_resp.json()
        n_ctx, model_alias = _n_ctx(props), props.get("model_alias", "")
        if n_ctx and model_alias and model_alias in cache:
            cache[model_alias]["context_length"] = n_ctx
        return
    native = model_metadata_http.get(base + "/models", headers=headers, timeout=5, verify=verify)
    if native.is_error:
        return
    for child in (native.json() or {}).get("data", [])[:16]:
        child_id = child.get("id") if isinstance(child, dict) else None
        if not child_id or child_id not in cache or (child.get("status") or {}).get("value") not in ("loaded", "ready"):
            continue
        pr = _props({"model": child_id, "autoload": "false"})  # it may have unloaded since /models
        child_ctx = _n_ctx(pr.json()) if not pr.is_error else None
        if child_ctx:
            cache[child_id]["context_length"] = child_ctx


def _endpoint_metadata_cache_key(base_url: str, api_key: object = "") -> str:
    """Digest scoping an endpoint snapshot to the active (context-local) profile, the normalized
    endpoint and the credential fingerprint. Raw credentials are never retained in keys."""
    from agent.model_metadata import _normalize_base_url, hashlib, json
    from agent.model_metadata_protocol import _credential_identity
    from hermes_constants import get_hermes_home
    # Callers on the pricing path can hand in a non-str endpoint; key on its text form.
    endpoint = _normalize_base_url(base_url if isinstance(base_url, str) else str(base_url or ""))
    return hashlib.sha256(json.dumps([str(get_hermes_home()), endpoint, _credential_identity(api_key)]).encode("utf-8")).hexdigest()


def _remember_endpoint_models(memo_key: str, cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Store a snapshot (caller holds ``_endpoint_model_metadata_lock``); oldest beyond the bound are evicted."""
    from agent.model_metadata import (
        _ENDPOINT_MODEL_CACHE_MAX_SIZE, _endpoint_model_metadata_cache, _endpoint_model_metadata_cache_time, time
    )
    _endpoint_model_metadata_cache[memo_key] = cache
    _endpoint_model_metadata_cache_time[memo_key] = time.time()
    while len(_endpoint_model_metadata_cache) > _ENDPOINT_MODEL_CACHE_MAX_SIZE:
        oldest = min(_endpoint_model_metadata_cache, key=lambda item: _endpoint_model_metadata_cache_time.get(item, 0))
        del _endpoint_model_metadata_cache[oldest]
        _endpoint_model_metadata_cache_time.pop(oldest, None)
    return cache


def _run_endpoint_metadata_fetch(memo_key, normalized, api_key, options, flight, background=False):
    """Leader fetch: probe, store the snapshot and release followers."""
    from agent.model_metadata import (
        _ENDPOINT_MODEL_CACHE_TTL, _endpoint_model_metadata_cache, _endpoint_model_metadata_cache_time, _endpoint_model_metadata_inflight, _endpoint_model_metadata_lock, logger, time
    )
    try:
        result = _fetch_endpoint_model_metadata_uncached(normalized, api_key, **options)
        with _endpoint_model_metadata_lock:
            previous = _endpoint_model_metadata_cache.get(memo_key)
            if background and not result and previous:
                # A failed refresh must not replace a good snapshot with an empty one: keep it
                # and retry in five minutes.
                _endpoint_model_metadata_cache_time[memo_key] = time.time() - _ENDPOINT_MODEL_CACHE_TTL + 300
                flight.result = previous
                return previous
            flight.result = result
            _remember_endpoint_models(memo_key, result)
        return result
    except Exception:
        # A background refresh has no caller to raise to; keep the old snapshot.
        if background:
            logger.debug("Background endpoint metadata refresh failed for %s", normalized, exc_info=True)
            return {}
        raise
    finally:
        # Also release followers after cancellation/BaseException. They get an unknown snapshot,
        # and a later call may retry; no failure is invented.
        with _endpoint_model_metadata_lock:
            _endpoint_model_metadata_inflight.pop(memo_key, None)
            flight.done.set()


def _fetch_endpoint_model_metadata_uncached(
    normalized: str, api_key: str, *, native_protocol_probes: bool = True,
) -> dict[str, dict[str, Any]]:
    """The probe ladder; invoked once per in-flight metadata scope (callers store the result)."""
    from agent.model_metadata import Optional, _auth_headers, _endpoint_blackholed, _endpoint_disk_cache_put, _localhost_to_ipv4, _model_metadata_payloads, _note_if_connect_timeout, contextlib, detect_local_server_type, is_local_endpoint, logger, model_metadata_http
    import logging
    local = is_local_endpoint(normalized)
    alternate = normalized[:-3].rstrip("/") if normalized.endswith("/v1") else normalized + "/v1"
    candidates = [normalized] + ([alternate] if alternate != normalized else [])
    headers = _auth_headers(api_key)
    verify = model_metadata_http.resolve_verify(normalized)
    last_error: Optional[Exception] = None
    if native_protocol_probes and local:
        try:
            # Only LM Studio's loaded-instance context must precede the standard listing; the
            # full context fallback still detects the other local protocols when needed.
            if detect_local_server_type(normalized, api_key=api_key, lmstudio_only=True) == "lm-studio":
                return _lmstudio_native_models(normalized, headers)
        except Exception as exc:
            logging.getLogger(__name__).error("_fetch_endpoint_model_metadata_uncached: operation failed",
                exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
            last_error = exc
            _note_if_connect_timeout(exc, normalized)
    for candidate in candidates:
        # A connect timeout condemns the host, not the path.
        if _endpoint_blackholed(normalized):
            break
        # Cache keys stay unrewritten; only the outbound target is IPv4-resolved.
        request_candidate = _localhost_to_ipv4(candidate)
        url = request_candidate.rstrip("/") + "/models"
        try:
            with model_metadata_http.stream(url, headers=headers, timeout=(5, 10), verify=verify) as response:
                if response.status_code in (401, 403):
                    logger.debug("Model metadata probe received HTTP %s from %s; stopping candidate probing", response.status_code, url)
                    break
                response.raise_for_status()
                response.read()
                payload = response.json()
            cache = _model_metadata_payloads._parse_models_payload(payload)
            if native_protocol_probes and any(m.get("owned_by") == "llamacpp" for m in payload.get("data", []) if isinstance(m, dict)):
                with contextlib.suppress(Exception):
                    _apply_llamacpp_props(cache, request_candidate, headers, verify)
            if cache and not local:
                _endpoint_disk_cache_put(normalized, cache)
            return cache
        except Exception as exc:
            logging.getLogger(__name__).error("_fetch_endpoint_model_metadata_uncached: operation failed",
                exc_info=(RuntimeError, RuntimeError(type(exc).__name__), exc.__traceback__))
            last_error = exc
            _note_if_connect_timeout(exc, normalized)
    if last_error:
        logger.debug("Failed to fetch model metadata from %s/models: %s", normalized, last_error)
    return {}
