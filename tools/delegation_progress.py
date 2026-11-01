"""Bounded operational subagent status. Never forwards arguments or reasoning."""
import math
import re

def normalize_subagent_progress(event, payload):
    if not isinstance(payload, dict):
        return None
    if event == "subagent":
        state = payload.get("status")
        phase = {"queued": "spawn_requested", "running": "start", "completed": "complete",
                 "failed": "complete", "cancelled": "complete"}.get(state)
        if phase is None:
            return None
        event = "subagent." + phase
        payload = {**payload, "goal": payload.get("summary", "")}
    if not str(event).startswith("subagent."):
        return None
    identifier = payload.get("subagent_id") or payload.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", identifier):
        return None
    phase = str(event).split(".", 1)[1]
    states = {"spawn_requested": "queued", "queued": "queued", "start": "running",
              "tool_call": "running", "tool": "running", "progress": "running", "complete": "completed"}
    if phase not in states:
        return None
    status = states[phase]
    if phase == "complete":
        result = str(payload.get("status") or "").lower()
        if result in {"cancelled", "canceled", "interrupted"}:
            status = "cancelled"
        elif result not in {"ok", "success", "completed", "complete"}:
            status = "failed"
    data = {"id": identifier, "status": status}
    parent = payload.get("parent_id")
    if isinstance(parent, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", parent):
        data["parent_id"] = parent
    for field in ("task_index", "task_count", "tool_count"):
        value = payload.get(field, 0)
        data[field] = max(0, min(value, 100000)) if type(value) is int else 0
    duration = payload.get("duration_seconds")
    if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
        data["duration_seconds"] = duration
    # A short assigned-work label is operational context, never child analysis.
    goal = str(payload.get("goal") or "").splitlines()
    label = goal[0] if goal else ""
    label = re.sub(r"(?i)(bearer\s+|(?:api[_ -]?key|token|password|secret)\s*[:=]\s*)\S+", r"\1[redacted]", label)
    label = re.sub(r"(?i)\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]+", "[redacted]", label)
    data["summary"] = " ".join(label.split())[:120] or "Delegated task"
    return data
