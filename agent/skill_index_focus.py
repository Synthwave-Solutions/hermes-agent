"""Keep the always-on skill index proportional to what is actually used.

The index lists every installed skill with its description in every prompt.
That is right while a catalogue is small and wrong once it is not: on this
workstation 1,429 skills render a 136 KB index, of which one imported bulk
collection (863 skills, 2.4% of them ever opened) is 45%. Every turn pays for
it in prompt-processing time and in window space that the conversation then
does not have.

The rule here is deliberately blunt and evidence-based: a large category in
which almost nothing has ever been opened is demoted to names only. It is the
same demotion the coding posture already applies, driven by usage instead of
by posture, and it inherits that design's hard rule: **nothing is ever
hidden**. Every skill name stays in the index and stays loadable through
``skill_view`` and ``skills_list``; only descriptions are dropped, so a model
can still see that a skill exists and go read it.

Off by default (``skills.index_mode: full``). Set ``skills.index_mode: usage``
to turn it on.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A category has to be big before its share means anything: three unused
# skills out of four is noise, eight hundred out of eight hundred and sixty
# is a catalogue nobody reads.
DEFAULT_MIN_CATEGORY_SIZE = 20
# Below this share of ever-opened skills, the descriptions are not earning
# their place in every prompt.
DEFAULT_USED_SHARE_THRESHOLD = 0.10
# Recent activity protects a category, but proportionally: in a catalogue of
# eight hundred skills, nine recent views is not "someone works here", it is
# noise. An absolute exemption at that size would keep the whole collection in
# full forever, which is the exact problem this exists to solve.
DEFAULT_RECENT_DAYS = 30


def _config(config: Optional[dict[str, Any]]) -> dict:
    if config is not None:
        return config or {}
    try:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}
    except Exception:
        logger.debug("skill index focus: config unavailable", exc_info=True)
        return {}


def index_mode(config: Optional[dict[str, Any]] = None) -> str:
    """``full`` (default, today's behaviour) or ``usage``."""
    raw = ((_config(config).get("skills") or {}).get("index_mode") or "full")
    mode = str(raw).strip().lower()
    return "usage" if mode in {"usage", "used", "lean", "smart"} else "full"


def _usage_map(skills_dir: Path) -> dict:
    try:
        raw = (skills_dir / ".usage.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_ts(value) -> float:
    """Epoch seconds for an ISO timestamp, 0.0 when unreadable."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _skill_identifiers(skill_md: Path) -> tuple[str, str]:
    """(directory name, declared name) so either key matches the usage file.

    The usage file is keyed by a skill's declared name, which is often not its
    directory name. Matching on only one of the two silently reports a used
    skill as unused, which would demote a category somebody relies on.
    """
    directory = skill_md.parent.name
    declared = directory
    try:
        head = skill_md.read_text(encoding="utf-8", errors="replace")[:1500]
    except OSError:
        return directory, declared
    for line in head.splitlines():
        if line.startswith("name:"):
            declared = line[5:].strip().strip("\"'") or directory
            break
    return directory, declared


def compact_categories(
    skills_dir: "Path | str | None" = None,
    *,
    config: Optional[dict[str, Any]] = None,
    now: Optional[float] = None,
) -> frozenset[str]:
    """Categories to render names-only, or an empty set when the mode is off.

    Never raises: a problem reading usage means the index renders in full,
    which is the behaviour that existed before this module.
    """
    try:
        if index_mode(config) != "usage":
            return frozenset()
        cfg_skills = _config(config).get("skills") or {}
        min_size = int(cfg_skills.get("index_min_category_size", DEFAULT_MIN_CATEGORY_SIZE))
        threshold = float(cfg_skills.get("index_used_share", DEFAULT_USED_SHARE_THRESHOLD))
        recent_days = float(cfg_skills.get("index_recent_days", DEFAULT_RECENT_DAYS))
        keep = {
            str(name).strip()
            for name in (cfg_skills.get("index_always_full") or [])
            if str(name).strip()
        }

        root = Path(skills_dir) if skills_dir else Path.home() / ".hermes" / "skills"
        if not root.is_dir():
            return frozenset()
        usage = _usage_map(root)
        if not usage:
            # No evidence at all is not evidence of disuse.
            return frozenset()
        cutoff = (now if now is not None else time.time()) - recent_days * 86400

        totals: dict[str, int] = {}
        touched: dict[str, int] = {}
        recent: dict[str, int] = {}
        pinned: set[str] = set()
        for skill_md in root.glob("*/*/SKILL.md"):
            category = skill_md.parent.parent.name
            totals[category] = totals.get(category, 0) + 1
            directory, declared = _skill_identifiers(skill_md)
            entry = usage.get(declared) or usage.get(directory)
            if not isinstance(entry, dict):
                continue
            if (entry.get("use_count") or 0) or (entry.get("view_count") or 0):
                touched[category] = touched.get(category, 0) + 1
            if entry.get("pinned"):
                pinned.add(category)
            last = max(_parse_ts(entry.get("last_used_at")), _parse_ts(entry.get("last_viewed_at")))
            if last and last >= cutoff:
                recent[category] = recent.get(category, 0) + 1

        def _is_dead(category: str, total: int) -> bool:
            # Both measures are shares of the category, so a big catalogue is
            # judged the same way a small one is: a handful of hits among
            # hundreds of skills does not keep hundreds of descriptions in
            # every prompt. A pin is an explicit human signal and always wins.
            if category in keep or category in pinned or total < min_size:
                return False
            ever = touched.get(category, 0) / total
            lately = recent.get(category, 0) / total
            return max(ever, lately) < threshold

        demote = {
            category for category, total in totals.items() if _is_dead(category, total)
        }
        if demote:
            logger.info(
                "skill index: rendering %d categories names-only (%d skills), "
                "each under %.0f%% ever opened",
                len(demote),
                sum(totals[c] for c in demote),
                threshold * 100,
            )
        return frozenset(demote)
    except Exception:  # pragma: no cover: the prompt must build regardless
        logger.debug("skill index focus failed; rendering the full index", exc_info=True)
        return frozenset()
