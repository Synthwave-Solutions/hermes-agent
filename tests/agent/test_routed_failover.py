"""Unit tests for the routed-failover compaction decision (SYNTHWAVE fork)."""

from types import SimpleNamespace

import pytest

from agent.context_compressor import ContextCompressor
from agent.error_classifier import FailoverReason
from agent.routed_failover import (
    failover_threshold_tokens,
    parse_failover_context_length,
    should_compact_for_routed_failover,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        (272000, 272000),
        ("272000", 272000),
        (0, None),
        (-5, None),
        (True, None),
        ("abc", None),
    ],
)
def test_parse_failover_context_length(raw, expected):
    assert parse_failover_context_length(raw) == expected


def _decide(**overrides):
    params = dict(
        reason=FailoverReason.rate_limit,
        retry_count=1,
        request_tokens=400_000,
        failover_context_length=272_000,
        failover_threshold=224_000,
        routed=True,
        compression_enabled=True,
    )
    params.update(overrides)
    return should_compact_for_routed_failover(**params)


def test_rate_limit_on_large_routed_request_compacts_immediately():
    assert _decide() is True


@pytest.mark.parametrize("reason", [FailoverReason.billing, FailoverReason.upstream_rate_limit])
def test_credit_and_upstream_limits_compact_immediately(reason):
    assert _decide(reason=reason) is True


@pytest.mark.parametrize(
    "reason", [FailoverReason.overloaded, FailoverReason.server_error, FailoverReason.timeout]
)
def test_transport_failures_get_one_plain_retry_first(reason):
    assert _decide(reason=reason, retry_count=1) is False
    assert _decide(reason=reason, retry_count=2) is True


def test_request_that_fits_the_fallbacks_is_left_alone():
    assert _decide(request_tokens=200_000) is False
    assert _decide(request_tokens=224_000) is False


def test_unconfigured_unrouted_or_disabled_never_compacts():
    assert _decide(failover_context_length=None) is False
    assert _decide(routed=False) is False
    assert _decide(compression_enabled=False) is False


@pytest.mark.parametrize(
    "reason",
    [FailoverReason.context_overflow, FailoverReason.format_error, FailoverReason.auth],
)
def test_errors_compaction_cannot_fix_are_ignored(reason):
    assert _decide(reason=reason, retry_count=3) is False


def test_threshold_matches_the_compressor_at_the_fallback_window():
    compressor = SimpleNamespace(
        max_tokens=8192,
        _base_threshold_percent=0.85,
        _effective_threshold_percent=ContextCompressor._effective_threshold_percent,
        _compute_threshold_tokens=ContextCompressor._compute_threshold_tokens,
    )
    expected = ContextCompressor._compute_threshold_tokens(
        272_000, ContextCompressor._effective_threshold_percent(272_000, 0.85), 8192,
    )
    assert failover_threshold_tokens(compressor, 272_000) == expected


def test_threshold_falls_back_for_engines_without_builtin_helpers():
    engine = SimpleNamespace(max_tokens=8000)
    assert failover_threshold_tokens(engine, 272_000) == int((272_000 - 8000) * 0.85)
