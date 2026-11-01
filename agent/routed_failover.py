"""Compact to the fallback window when a routed primary fails (SYNTHWAVE fork).

A router such as OmniRoute drops combo targets whose known context window is
too small for a request *before* dispatching it. When the primary target has
the larger window (for example a 1M-token Opus route, which lets the session
grow toward its 850K compression threshold) and then fails for a transient
reason (rate limit, exhausted credits, overload, timeout), the smaller
fallbacks were never tried and no target returned a context-overflow error.
The reactive compression path therefore never fires and the turn fails,
although the fallbacks would have answered a compacted request.

With ``compression.failover_context_length`` set to the window of those
fallback targets (272000 for the Codex models behind Opus), the conversation
loop compacts the history to that window once and retries; the router then
accepts the fallbacks again. Unset (the default), nothing changes.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.error_classifier import FailoverReason

# Provider-side failures a different target can absorb. Context overflow has
# its own path; auth, format and policy errors are not fixed by compaction.
_RATE_OR_CREDIT = frozenset({
    FailoverReason.rate_limit,
    FailoverReason.billing,
    FailoverReason.upstream_rate_limit,
})
_TRANSPORT_OR_SERVER = frozenset({
    FailoverReason.overloaded,
    FailoverReason.server_error,
    FailoverReason.timeout,
})

_FALLBACK_THRESHOLD_PERCENT = 0.85


def parse_failover_context_length(value: Any) -> Optional[int]:
    """Config value -> positive int window, or None when unset or invalid."""
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def failover_threshold_tokens(compressor: Any, failover_context_length: int) -> int:
    """Compaction trigger the compressor would use at the fallback window."""
    max_tokens = getattr(compressor, "max_tokens", None)
    try:
        percent = compressor._effective_threshold_percent(
            failover_context_length,
            getattr(compressor, "_base_threshold_percent", _FALLBACK_THRESHOLD_PERCENT),
        )
        return int(compressor._compute_threshold_tokens(
            failover_context_length, percent, max_tokens,
        ))
    except Exception:
        # Plugin context engines need not expose the built-in helpers.
        reserve = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else 0
        return int((failover_context_length - reserve) * _FALLBACK_THRESHOLD_PERCENT)


def should_compact_for_routed_failover(
    *,
    reason: Any,
    retry_count: int,
    request_tokens: int,
    failover_context_length: Optional[int],
    failover_threshold: int,
    routed: bool,
    compression_enabled: bool,
) -> bool:
    """True when a failed routed request is too large for its fallbacks.

    Rate limits and credit exhaustion do not recover within the retry
    window, so they compact on the first failure. Overload, server errors
    and timeouts first get one plain retry, mirroring the loop's eager
    fallback policy for transport failures.
    """
    if not failover_context_length or not routed or not compression_enabled:
        return False
    if request_tokens <= failover_threshold:
        return False
    if reason in _RATE_OR_CREDIT:
        return True
    if reason in _TRANSPORT_OR_SERVER:
        return retry_count >= 2
    return False
