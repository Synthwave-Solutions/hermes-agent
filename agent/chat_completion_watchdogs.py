"""Chat completion watchdogs.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agent.chat_completion_helpers import _NonStreamWatchdogs

def _resolve_nonstream_watchdogs(agent, api_kwargs: dict) -> _NonStreamWatchdogs:
    """Stale-call timeout plus the Codex Responses stream watchdogs.

    The stale detector kills a hung provider early so the retry loop can rotate
    credentials / fall back. Codex adds two failure modes: accepting the connection
    but never emitting an event (no-event TTFB cutoff; a reconnect succeeds in ~2s)
    and stalling after substantive model progress begins (event-idle gap; any parsed SSE
    event remains transport activity). Only the implicit official OpenAI Codex policy
    for large contexts defers arming until progress; small requests, compatible backends,
    and explicit overrides retain the legacy first-event semantics. Tunables:
    HERMES_CODEX_TTFB_TIMEOUT_SECONDS,
    HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS (0 disables each),
    HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS / HERMES_CODEX_TTFB_STRICT,
    HERMES_CODEX_TTFB_MAX_SECONDS (opt-in ceiling, default 0 = none), HERMES_CODEX_HARD_TIMEOUT_SECONDS.
    """
    from agent.chat_completion_helpers import (
        CODEX_FIRST_PROGRESS_TIMEOUT_SECONDS, _NonStreamWatchdogs, _bound_openai_codex_stale_timeout, _high_effort_silence_floor, _is_openai_codex_backend, _local_stream_stale_timeout_default, env_float, estimate_request_context_tokens, logger, openai_codex_stale_timeout_floor, os, uses_local_inference_patience
    )
    # The effort floor on the STALE timeout lives inside _compute_non_stream_stale_timeout so the
    # run-budget cap still bounds it; here the floor only raises the TTFB/idle implicit defaults.
    stale_timeout = agent._compute_non_stream_stale_timeout(api_kwargs)
    codex = agent.api_mode == "codex_responses"
    openai_codex_backend = _is_openai_codex_backend(agent)
    est_tokens = estimate_request_context_tokens(api_kwargs)
    effort_floor = _high_effort_silence_floor(agent) if codex else 0.0
    codex_floor = 0.0
    # Local Responses servers keep their configured local stale/TTFB grace: the hosted
    # large-context floor, hard ceiling and TTFB scale-up/cap below must not tighten it.
    base_url = getattr(agent, "base_url", None)
    local = uses_local_inference_patience(agent, base_url)
    if codex and not local:
        codex_floor = openai_codex_stale_timeout_floor(est_tokens)
        stale_timeout = _bound_openai_codex_stale_timeout(stale_timeout, est_tokens)

    idle_default = max(effort_floor, next(
        (default for threshold, default in ((100_000, 180.0), (50_000, 120.0), (10_000, 60.0)) if est_tokens > threshold),
        12.0))

    # No-event TTFB cutoff. Default 120s: the SDK's own read timeout is 600s,
    # and a tight 12s killed subscription-backed requests mid-prefill.
    ttfb_enabled = codex
    ttfb_explicit = env_float("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", -1.0) != -1.0
    ttfb_timeout = env_float("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", 120.0)
    if ttfb_timeout <= 0:
        ttfb_enabled = False
    elif codex and not local:
        # Large requests legitimately spend tens of seconds in admission/prefill before the
        # first SSE event: scale the cutoff up to the idle default unless TTFB_STRICT is set.
        disable_above = env_float("HERMES_CODEX_TTFB_DISABLE_ABOVE_TOKENS", 10_000.0)
        strict = os.environ.get("HERMES_CODEX_TTFB_STRICT", "").strip().lower() in {"1", "true", "yes", "on"}
        if not strict and disable_above > 0 and est_tokens >= disable_above and ttfb_timeout < idle_default:
            logger.info("Scaling codex-responses no-event TTFB watchdog from %.0fs to %.0fs "
                "for large request (context=~%s tokens >= %.0f). "
                "Set HERMES_CODEX_TTFB_STRICT=1 to keep the smaller cutoff.", ttfb_timeout, idle_default,
                f"{est_tokens:,}", disable_above)
            ttfb_timeout = idle_default
        # Opt-in ceiling (0 = off): a 120s default here silently undid the scale-up above (#91621).
        ttfb_cap = env_float("HERMES_CODEX_TTFB_MAX_SECONDS", 0.0)
        if ttfb_cap > 0 and ttfb_timeout > ttfb_cap:
            logger.info("Capping codex-responses no-event TTFB timeout from %.0fs to %.0fs "
                "(context=~%s tokens) per HERMES_CODEX_TTFB_MAX_SECONDS.", ttfb_timeout, ttfb_cap,
                f"{est_tokens:,}")
            ttfb_timeout = ttfb_cap
    elif not ttfb_explicit and local:
        # A local server prefills for minutes before its first event; the chat-completions
        # siblings already grant local endpoints the local stale ceiling, so the Responses
        # transport gets the same grace instead of the 120s hosted cutoff (#92302).
        local_ceiling = _local_stream_stale_timeout_default()
        if local_ceiling > ttfb_timeout:
            logger.info("Local provider detected (%s) — no-event TTFB watchdog raised from %.0fs to %.0fs "
                "(agent.local_stream_stale_timeout); set HERMES_CODEX_TTFB_TIMEOUT_SECONDS for an explicit cutoff.",
                base_url, ttfb_timeout, local_ceiling)
            ttfb_timeout = local_ceiling
    if ttfb_enabled and not ttfb_explicit:
        # High-effort thinking precedes the first event; the floor outranks the cap.
        ttfb_timeout = max(ttfb_timeout, effort_floor)

    # An operator-set idle timeout keeps first-event semantics; only the implicit
    # default defers arming until model progress. Sentinel: env_float returns the
    # default for unset AND unparseable values, so both count as implicit.
    idle_explicit = env_float("HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS", -1.0) != -1.0
    idle_timeout = env_float("HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS", idle_default)
    progress_gated = codex and openai_codex_backend and codex_floor > 0 and not idle_explicit
    return _NonStreamWatchdogs(stale_timeout=stale_timeout, codex=codex, est_tokens=est_tokens,
        ttfb_enabled=ttfb_enabled, ttfb_timeout=ttfb_timeout, idle_enabled=codex and idle_timeout > 0,
        idle_timeout=idle_timeout, idle_requires_progress=progress_gated,
        # A lifecycle frame proves transport liveness, not model progress. Bound that phase
        # from the physical-attempt start; events cannot restart the grace period.
        progress_timeout=CODEX_FIRST_PROGRESS_TIMEOUT_SECONDS if progress_gated else 0.0)
