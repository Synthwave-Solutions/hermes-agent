"""Notification policy for scheduled jobs: the single place that decides.

A job carries one engine-owned field ``notify`` with two independent levels:

* ``in_app``: toasts, badges, unread markers and desktop notifications in
  the WebUI. API values ``all``, ``failures``, ``quiet``, ``off``. The UI
  offers the first three ("Every run", "Only failures", "Never"); ``off`` is
  set only through the API or by cron admins.
* ``delivery``: chat and email delivery by the engine. API values ``all``,
  ``failures``, ``off``.

A job without ``notify`` (legacy) keeps its old behaviour: in-app follows
``toast_notifications`` and delivery is always on.

Muting never touches incident recording, admin capacity alerts or the
central error feed; callers apply this policy to user-facing notifications
only.

This module imports only the standard library and has no package-relative
imports, so it can be loaded on its own. The WebUI ships a byte-identical
copy (``api/_notify_policy_fallback.py``) whose sha256 is pinned in a test:
never change one without the other.
"""

from __future__ import annotations

__all__ = [
    "LEVELS_IN_APP",
    "LEVELS_DELIVERY",
    "UI_IN_APP_CHOICES",
    "ALERT_KINDS",
    "OUTCOME_MUTED",
    "MANAGED_BY",
    "normalize_notify",
    "resolve_notify",
    "default_notify_for_new_job",
    "should_deliver",
    "in_app_flags",
    "combine_flags",
    "legacy_toast_value",
    "normalize_managed_by",
]

LEVELS_IN_APP = ("all", "failures", "quiet", "off")
LEVELS_DELIVERY = ("all", "failures", "off")
UI_IN_APP_CHOICES = ("all", "failures", "quiet")  # "Every run", "Only failures", "Never"; "off" is API and cron admin only
ALERT_KINDS = ("failure", "blocked_config", "drift", "preflight")
OUTCOME_MUTED = "suppressed_muted"                # engine delivery_outcome value
MANAGED_BY = ("synthwave", "client")

_LEGACY_DEFAULTS = {"in_app": "all", "delivery": "all"}
_VALID_LEVELS = {"in_app": LEVELS_IN_APP, "delivery": LEVELS_DELIVERY}
_FLAG_KEYS = ("toast", "badge", "desktop")
_NO_FLAGS = {"toast": False, "badge": False, "desktop": False}

# in_app level -> (flags for a successful run, flags for a failed run).
# Silent runs never notify, whatever the level.
_IN_APP_TABLE = {
    "all": (
        {"toast": True, "badge": True, "desktop": False},
        {"toast": True, "badge": True, "desktop": True},
    ),
    "failures": (
        {"toast": False, "badge": False, "desktop": False},
        {"toast": True, "badge": True, "desktop": True},
    ),
    "quiet": (
        {"toast": False, "badge": True, "desktop": False},
        {"toast": False, "badge": True, "desktop": False},
    ),
    "off": (
        {"toast": False, "badge": False, "desktop": False},
        {"toast": False, "badge": False, "desktop": False},
    ),
}


def normalize_notify(value) -> dict | None:
    """None stays None. A dict with only known keys and valid levels is returned
    with both keys filled from the legacy defaults. Anything else raises ValueError."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("notify must be an object with in_app and delivery")
    if any(key not in _VALID_LEVELS for key in value):
        raise ValueError("notify accepts only the keys in_app and delivery")
    result = dict(_LEGACY_DEFAULTS)
    for key, allowed in _VALID_LEVELS.items():
        if key not in value:
            continue
        level = value[key]
        if not isinstance(level, str) or level not in allowed:
            raise ValueError(
                "notify.%s must be one of %s" % (key, ", ".join(allowed))
            )
        result[key] = level
    return result


def resolve_notify(job: dict) -> dict:
    """{"in_app": ..., "delivery": ..., "source": "explicit" | "legacy"}.
    Legacy: in_app = "quiet" if job.get("toast_notifications") is False else "all";
    delivery = "all"."""
    if not isinstance(job, dict):
        job = {}
    raw = job.get("notify")
    if raw is not None:
        try:
            explicit = normalize_notify(raw)
        except ValueError:
            # A damaged record reads as legacy, so one bad value never breaks
            # listing or delivery. Writes go through normalize_notify and fail.
            explicit = None
        if explicit is not None:
            return {
                "in_app": explicit["in_app"],
                "delivery": explicit["delivery"],
                "source": "explicit",
            }
    return {
        "in_app": "quiet" if job.get("toast_notifications") is False else "all",
        "delivery": "all",
        "source": "legacy",
    }


def default_notify_for_new_job(*, category: str | None) -> dict:
    """{"in_app": "failures" if category == "Monitoring" else "all", "delivery": "all"}."""
    return {
        "in_app": "failures" if category == "Monitoring" else "all",
        "delivery": "all",
    }


def should_deliver(job: dict, *, success: bool, alert_kind: str | None = None) -> tuple[bool, str | None]:
    """delivery all: (True, None); failures: (True, None) when not success or
    alert_kind in ALERT_KINDS, else (False, OUTCOME_MUTED); off: (False, OUTCOME_MUTED)."""
    delivery = resolve_notify(job)["delivery"]
    if delivery == "all":
        return True, None
    if delivery == "failures":
        if not success or alert_kind in ALERT_KINDS:
            return True, None
        return False, OUTCOME_MUTED
    return False, OUTCOME_MUTED


def in_app_flags(level: str, *, success: bool, silent: bool) -> dict:
    """{"toast": bool, "badge": bool, "desktop": bool} per the table in 3.1.
    Silent runs give all False. desktop is True only for toast-eligible failures."""
    if not isinstance(level, str) or level not in _IN_APP_TABLE:
        raise ValueError("in_app level must be one of " + ", ".join(LEVELS_IN_APP))
    if silent:
        return dict(_NO_FLAGS)
    on_success, on_failure = _IN_APP_TABLE[level]
    return dict(on_success if success else on_failure)


def combine_flags(*flag_sets: dict) -> dict:
    """AND of the given flag dicts (job level and viewer level)."""
    if not flag_sets:
        # Nothing to combine: fail closed rather than notify.
        return dict(_NO_FLAGS)
    return {
        key: all(bool(flags.get(key, False)) for flags in flag_sets)
        for key in _FLAG_KEYS
    }


def legacy_toast_value(notify: dict) -> bool:
    """in_app in ("all", "failures")."""
    normalized = normalize_notify(notify)
    if normalized is None:
        raise ValueError("notify must be an object with in_app and delivery")
    return normalized["in_app"] in ("all", "failures")


def normalize_managed_by(value) -> str:
    """None or "" gives "client"; a value in MANAGED_BY is returned; anything else raises ValueError."""
    if value is None or value == "":
        return "client"
    if isinstance(value, str) and value in MANAGED_BY:
        return value
    raise ValueError("managed_by must be one of " + ", ".join(MANAGED_BY))
