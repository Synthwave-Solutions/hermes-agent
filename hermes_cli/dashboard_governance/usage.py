from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from utils import atomic_json_write

from .context import DashboardGovernanceContext
from .models import AccessDecision

_FILE_WRITE_TOOLS = {"write_file", "patch"}


def _usage_file() -> Path:
    return get_hermes_home() / "dashboard-governance-usage.json"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _subject_key(ctx: DashboardGovernanceContext) -> str:
    subject = ctx.access.subject or ctx.subject
    raw = (subject.normalized_email or subject.user_id or "anonymous").strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _counter_bucket(state: dict[str, Any], ctx: DashboardGovernanceContext) -> dict[str, int]:
    day = _today()
    subject = _subject_key(ctx)
    days = state.setdefault("days", {})
    if not isinstance(days, dict):
        state["days"] = days = {}
    day_bucket = days.setdefault(day, {})
    if not isinstance(day_bucket, dict):
        days[day] = day_bucket = {}
    counters = day_bucket.setdefault(subject, {})
    if not isinstance(counters, dict):
        day_bucket[subject] = counters = {}
    counters.setdefault("tool_calls", 0)
    counters.setdefault("file_writes", 0)
    return counters


def _cap(caps: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = caps.get(name)
        if value is None or value == "":
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def _usage_counter_for_tool(tool_name: str) -> str:
    if tool_name in _FILE_WRITE_TOOLS:
        return "file_writes"
    return "tool_calls"


def check_usage_caps(ctx: DashboardGovernanceContext | None, tool_name: str) -> AccessDecision:
    if ctx is None or ctx.access.mode != "enforce":
        return AccessDecision(True, "governance_inactive")
    caps = dict(ctx.access.grants.usage_caps or {})
    if not caps:
        return AccessDecision(True, "usage_caps_inactive")

    state = _load_state(_usage_file())
    counters = _counter_bucket(state, ctx)

    daily_tool_calls = _cap(caps, "daily_tool_calls", "max_daily_tool_calls", "tool_calls_daily")
    if daily_tool_calls is not None and int(counters.get("tool_calls", 0)) >= daily_tool_calls:
        return AccessDecision(False, "daily_tool_calls_exceeded")

    if tool_name in _FILE_WRITE_TOOLS:
        daily_file_writes = _cap(caps, "daily_file_writes", "max_daily_file_writes", "file_writes_daily")
        if daily_file_writes is not None and int(counters.get("file_writes", 0)) >= daily_file_writes:
            return AccessDecision(False, "daily_file_writes_exceeded")

    return AccessDecision(True, "usage_allowed")


def record_tool_usage(ctx: DashboardGovernanceContext | None, tool_name: str) -> None:
    if ctx is None or ctx.access.mode != "enforce":
        return
    if not dict(ctx.access.grants.usage_caps or {}):
        return
    path = _usage_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state(path)
    counters = _counter_bucket(state, ctx)
    counters["tool_calls"] = int(counters.get("tool_calls", 0)) + 1
    if _usage_counter_for_tool(tool_name) == "file_writes":
        counters["file_writes"] = int(counters.get("file_writes", 0)) + 1
    atomic_json_write(path, state)
