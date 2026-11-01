"""Non-streaming chat completions that still feed response headers to the credits capture."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _chat_completion_capturing_headers(agent, client, api_kwargs: dict):
    """Non-streaming ``chat.completions.create`` that also feeds the response headers to the
    rate-limit / credits capture, as ``_chat_stream_created`` does for streams.

    The parsed ``ChatCompletion`` drops headers, so without this a non-streaming turn never
    observes ``x-nous-credits-*`` (usage-window ``spend_source`` / gauges go stale). The SDK's
    ``with_raw_response`` returns the same request with headers attached; a client without it keeps
    the plain call. Credit capture is limited to usage-window plans; real OpenAI SDK
    responses also retain OmniRoute's resolved model/provider headers for context sizing.
    Other client facades keep their plain call. A credit capture failure never fails the turn.
    """
    # `is True`: a Mock agent's invented attribute is not a window plan.
    state = getattr(agent, "_credits_state", None)
    capture_credits = getattr(agent, "provider", "") == "nous" and getattr(state, "is_usage_window", False) is True
    completions = client.chat.completions
    # SynthPulse also needs OmniRoute's actual model/provider headers to choose the
    # compressor window. Preserve SDK instrumentation and stand-in client behavior.
    capture_route = type(completions).__module__.startswith("openai.")
    if not capture_credits and not capture_route:
        return client.chat.completions.create(**api_kwargs)
    raw_api = getattr(getattr(getattr(client, "chat", None), "completions", None), "with_raw_response", None)
    if raw_api is None:
        return client.chat.completions.create(**api_kwargs)
    raw = raw_api.create(**api_kwargs)
    parse = getattr(raw, "parse", None)
    if not callable(parse):
        return raw
    if capture_route:
        from agent.chat_completion_gateway import _extract_omniroute_route
        route = _extract_omniroute_route(raw)
        if route:
            agent._omniroute_route_info = route
    from agent.credits_tracker import response_is_usage_window
    if not capture_credits or not response_is_usage_window(raw):
        return raw.parse()
    for capture in (agent._capture_rate_limits, agent._capture_credits,
                    agent._capture_nous_model_switch, agent._check_openrouter_cache_status):
        try:
            capture(raw)
        except Exception:
            logger.debug("non-streaming header capture failed", exc_info=True)
    return raw.parse()
