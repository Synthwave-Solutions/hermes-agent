"""Model metadata disk.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

def _load_json_dict(path: Path) -> dict[str, Any]:
    """JSON object at ``path``, or {} when missing/invalid."""
    from agent.model_metadata import (
        json
    )
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _ttl_memo_get(path: Path, key: str, ttl: float, *, ts_key: str, value_key: str) -> Optional[Any]:
    """Fresh (< ``ttl`` seconds) ``value_key`` of the ``key`` entry in a TTL memo file, else None."""
    from agent.model_metadata import (
        time
    )
    entry = _load_json_dict(path).get(key)
    if not isinstance(entry, dict):
        return None
    try:
        if (time.time() - float(entry[ts_key])) >= ttl:
            return None
        return entry[value_key]
    except Exception:
        return None


def _ttl_memo_put(path: Path, key: str, value: Any, ttl: float, *, ts_key: str, value_key: str, what: str, ts_first: bool = False) -> None:
    """Write ``key`` into a TTL memo file, dropping expired siblings. Best-effort."""
    from agent.model_metadata import (
        atomic_json_write, logger, time
    )
    try:
        now = time.time()
        data = {k: v for k, v in _load_json_dict(path).items() if isinstance(v, dict) and (now - float(v.get(ts_key, 0))) < ttl}
        data[key] = {ts_key: now, value_key: value} if ts_first else {value_key: value, ts_key: now}
        atomic_json_write(path, data, indent=0, separators=(",", ":"))
    except Exception as e:
        logger.debug("Failed to save %s: %s", what, e)
