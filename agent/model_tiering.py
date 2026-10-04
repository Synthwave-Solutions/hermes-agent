"""Per-turn model-tier and reasoning-effort selection (SYNTHWAVE fork).

When the session model is the OmniRoute combo ``synthwave-auto``, every turn
is classified on two INDEPENDENT axes before the API call:

  tier   : which model size the step needs (luna < terra < sol), mapped to an
           OmniRoute combo so failover chains stay intact:
             luna  -> synthwave-luna
             terra -> synthwave-auto   (terra-first chain)
             sol   -> synthwave-sol
  effort : how much reasoning the step needs (low/medium/high/xhigh),
           written into extra_body.reasoning.effort.

The axes are deliberately decoupled: a huge-context but trivial step can run
sol-low, a short but genuinely hard question can run terra-xhigh or luna-xhigh.

Kill switch: HERMES_MODEL_TIERING=0 (or config model.tiering: false).
Fail-open: any error leaves the original kwargs untouched.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

TIERED_SOURCE_MODELS = {"synthwave-auto"}

TIER_TO_MODEL = {
    "luna": "synthwave-luna",
    "terra": "synthwave-auto",
    "sol": "synthwave-sol",
}

# Explicit deep-thinking requests from the user.
_XHIGH_RE = re.compile(
    r"\b(think\s+(?:very\s+)?(?:hard|deep(?:ly)?)|ultrathink|denk\s+(?:heel\s+)?(?:diep|hard)"
    r"|maximum\s+reasoning|xhigh)\b", re.I)
# Work where reasoning depth genuinely pays off.
_HIGH_RE = re.compile(
    r"\b(architect(?:ure|uur)?|security\s*review|root\s*cause|design\s+(?:a|the|een)"
    r"|waarom\s+faalt|why\s+does\s+.{0,40}\s+fail|deadlock|race\s*condition"
    r"|migration\s+plan|migratieplan|proof|bewijs|complex)\b", re.I)
# Ordinary coding / editing / analysis work.
_MEDIUM_RE = re.compile(
    r"\b(implement|refactor|debug|fix|bouw|build|schrijf|write|analyseer|analy[sz]e"
    r"|deploy|genereer|generate|maak\s+(?:een|de|het)|create|review)\b", re.I)
# Capability cues that want the big model regardless of thinking depth.
_SOL_RE = re.compile(
    r"\b(productie|production|klant|client[-\s]facing|deliverable|volledige?\s+(?:app|rapport|document)"
    r"|hele\s+codebase|end[-\s]to[-\s]end|architect\w*)\b", re.I)

_ERROR_MARKER_RE = re.compile(r"(traceback|error|failed|exception|denied)", re.I)


def _msg_text(msg: Any) -> str:
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content or "")


def classify_turn(api_messages: list) -> Tuple[str, str, str]:
    """Return (tier, effort, reason) for the upcoming call."""
    last_user = ""
    recent_error_count = 0
    approx_chars = 0
    for msg in api_messages:
        if not isinstance(msg, dict):
            continue
        text = _msg_text(msg)
        approx_chars += len(text)
    for msg in reversed(api_messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user" and not last_user:
            last_user = _msg_text(msg)
        if role == "tool" and recent_error_count < 5:
            if _ERROR_MARKER_RE.search(_msg_text(msg)[:2000]):
                recent_error_count += 1
        if last_user:
            break
    approx_tokens = approx_chars // 4
    has_code = "```" in last_user or bool(re.search(r"\b(def |class |function|SELECT |import )", last_user))

    # -- effort (thinking depth) --
    if _XHIGH_RE.search(last_user):
        effort, ereason = "xhigh", "explicit deep-think request"
    elif _HIGH_RE.search(last_user) or recent_error_count >= 2:
        effort, ereason = "high", ("hard-problem cues" if recent_error_count < 2 else "repeated failures")
    elif _MEDIUM_RE.search(last_user) or has_code:
        effort, ereason = "medium", "build/edit work"
    else:
        effort, ereason = "low", "light turn"

    # -- tier (model capability) --
    if approx_tokens > 150_000 or _SOL_RE.search(last_user) or (has_code and effort in ("high", "xhigh")):
        tier, treason = "sol", ("large context" if approx_tokens > 150_000 else "heavy capability cues")
    elif len(last_user) < 300 and not has_code and approx_tokens < 30_000 and effort == "low":
        tier, treason = "luna", "short simple turn"
    else:
        tier, treason = "terra", "default"

    return tier, effort, f"{treason} / {ereason}"


def apply_model_tiering(agent, kwargs: dict, api_messages: list) -> None:
    """Mutate *kwargs* in place with the per-turn tier and effort."""
    try:
        if os.getenv("HERMES_MODEL_TIERING", "1").strip().lower() in ("0", "false", "off"):
            return
        model = str(kwargs.get("model") or "")
        if model not in TIERED_SOURCE_MODELS:
            return
        tier, effort, reason = classify_turn(api_messages or [])
        target = TIER_TO_MODEL.get(tier, model)
        kwargs["model"] = target
        extra = kwargs.get("extra_body")
        if not isinstance(extra, dict):
            extra = {}
            kwargs["extra_body"] = extra
        reasoning = extra.get("reasoning")
        if not isinstance(reasoning, dict):
            reasoning = {"enabled": True}
            extra["reasoning"] = reasoning
        reasoning["effort"] = effort
        logger.info("model_tiering: %s -> %s effort=%s (%s)", model, target, effort, reason)
    except Exception as exc:  # fail-open: never break the turn
        logger.debug("model_tiering skipped: %s", exc)
