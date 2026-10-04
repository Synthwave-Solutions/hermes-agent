from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_SECRET_KEY_RE = re.compile(r"(api[_-]?key|secret|password|passwd|token|authorization|credential|refresh)", re.I)
_BEARER_RE = re.compile(r"bearer\s+[A-Za-z0-9._~+/=-]+", re.I)


def _audit_file() -> Path:
    return get_hermes_home() / "dashboard-governance-audit.jsonl"


def _hash_identity(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _redact(value: Any, *, key: str = "") -> Any:
    if _SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, key=key) for v in value]
    if isinstance(value, tuple):
        return [_redact(v, key=key) for v in value]
    if isinstance(value, str):
        return _BEARER_RE.sub("Bearer [REDACTED]", value)
    return value


def append_audit_event(
    event: str,
    *,
    subject_email: str = "",
    subject_user_id: str = "",
    path: str = "",
    method: str = "",
    reason: str = "",
    mode: str = "",
    report_only: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": str(event),
        "subject_email_hash": _hash_identity(subject_email),
        "subject_user_id_hash": _hash_identity(subject_user_id),
        "path": str(path),
        "method": str(method).upper(),
        "reason": str(reason),
        "mode": str(mode),
        "report_only": bool(report_only),
        "extra": _redact(extra or {}),
    }
    path_obj = _audit_file()
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    with path_obj.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def read_audit_events(*, limit: int = 100) -> list[dict[str, Any]]:
    path_obj = _audit_file()
    try:
        lines = path_obj.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(lines[-max(1, int(limit)) :]):
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows
