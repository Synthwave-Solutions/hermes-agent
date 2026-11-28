"""Prompt-based model and effort routing for the SynthPulse automatic model.

The classifier uses the owning agent's gateway client and configured candidate
catalog. Explicit model/effort choices and provider recovery remain authoritative.
No prompt messages, tools or agent model attributes are changed.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging
from typing import Any
from copy import deepcopy

logger = logging.getLogger(__name__)

_REQUEST_USER = ContextVar("model_tiering_request_user", default=None)

TIERED_SOURCE_MODELS = {"synthwave-auto"}


def _msg_text(msg: Any) -> str:
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "")


@contextmanager
def tiering_request_user(agent, messages: list, original_user_message: Any):
    """Bind trusted clean input only for this request builder, never to prompt/history.

    The persistence anchor follows compaction. A newer user row (e.g. /steer)
    takes precedence, so an earlier clean ask cannot conceal a changed task.
    Outside the turn loop, request builders retain their existing wire fallback.
    """
    clean = None
    index = getattr(agent, "_persist_user_message_idx", None)
    if (
        isinstance(index, int)
        and not isinstance(index, bool)
        and isinstance(messages, list)
    ):
        latest = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if isinstance(messages[i], dict) and messages[i].get("role") == "user"
            ),
            None,
        )
        if latest == index and isinstance(original_user_message, (str, list)):
            clean = _msg_text({"content": original_user_message})
    token = _REQUEST_USER.set(clean)
    try:
        yield
    finally:
        _REQUEST_USER.reset(token)


def configure_model_tiering(agent, config: dict) -> None:
    """Snapshot the owning profile's switch at construction, without per-call I/O."""
    from hermes_cli.synthpulse_settings import enabled

    agent._model_tiering_enabled = enabled("model.tiering", True, config=config)
    agent._semantic_routing_config = deepcopy(
        (config.get("model") or {}).get("routing") or {}
    )


def _automatic_effort_allowed(agent) -> bool:
    # The standard request builder already resolves explicit user/profile effort
    # and provider recovery. Do not undo disabled thinking, a rejected effort,
    # or a one-shot reasoning-off continuation here.
    if getattr(agent, "reasoning_config", None) is not None:
        return False
    if any(
        getattr(agent, key, False)
        for key in (
            "_reasoning_effort_rejected",
            "_reasoning_disable_rejected",
        )
    ):
        return False
    wire = getattr(agent, "_wire_reasoning_config", None)
    if isinstance(wire, dict) and (
        wire.get("enabled") is False or wire.get("effort") == "none"
    ):
        return False
    overrides = getattr(agent, "request_overrides", None)
    if isinstance(overrides, dict):
        for layer in (overrides, overrides.get("extra_body")):
            if isinstance(layer, dict) and any(
                k in layer
                for k in (
                    "reasoning",
                    "reasoning_effort",
                    "thinking",
                    "enable_thinking",
                )
            ):
                return False
    return True


def apply_model_tiering(agent, kwargs: dict, api_messages: list) -> None:
    """Apply one prompt-classified route, preserving explicit user settings."""
    try:
        if not hasattr(agent, "_model_tiering_enabled"):
            configure_model_tiering(agent, {})
        if agent._model_tiering_enabled is False:
            return
        model = str(kwargs.get("model") or "")
        if model not in TIERED_SOURCE_MODELS:
            return
        from agent.semantic_model_routing import select_route

        decision = select_route(
            agent, api_messages or [], clean_user=_REQUEST_USER.get()
        )
        if decision is None:
            return
        target, effort = decision["model"], decision["effort"]
        kwargs["model"] = target
        agent._semantic_route_decision = dict(decision)
        if not _automatic_effort_allowed(agent):
            logger.info(
                "model_tiering: %s -> %s (preserving resolved reasoning)", model, target
            )
            return
        extra = kwargs.get("extra_body")
        if not isinstance(extra, dict):
            extra = {}
            kwargs["extra_body"] = extra
        reasoning = extra.get("reasoning")
        if not isinstance(reasoning, dict):
            reasoning = {"enabled": True}
            extra["reasoning"] = reasoning
        reasoning["effort"] = effort
        # OpenAI-compatible transports may serialize the same effort as a flat
        # field. The Claude bridge gives that field precedence when present.
        for fields in (kwargs, extra):
            if "reasoning_effort" in fields:
                fields["reasoning_effort"] = effort
        agent._wire_reasoning_config = dict(reasoning)
        logger.info("model_tiering: %s -> %s effort=%s", model, target, effort)
    except Exception as exc:  # fail-open: never break the turn
        logging.getLogger(__name__).error(
            "apply_model_tiering: operation failed",
            exc_info=(
                RuntimeError,
                RuntimeError(type(exc).__name__),
                exc.__traceback__,
            ),
        )
        logger.debug("model_tiering skipped: %s", exc)
