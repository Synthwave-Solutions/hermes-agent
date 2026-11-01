"""synthwave-policy: AUTO / ASK / BLOCK autonomy policy for every Hermes tool call.

Registers a ``pre_tool_call`` hook (the decision) and a ``post_tool_call`` observer (it only
remembers the page a browser session landed on, for the send-surface rules). Policy files:

* ``~/.hermes/policy/autonomy.yaml`` (the root file) is authoritative. Per-profile settings live
  under its ``profiles:`` key.
* ``$HERMES_HOME/policy/autonomy.yaml`` of a profile is optional and can only TIGHTEN: both files are
  evaluated and the stricter decision wins; its mode only counts when it says enforce; its own
  ``profiles:`` key is ignored.

Modes:

* observe (default): never interferes. Every would-be ASK or BLOCK is appended as one JSON line
  to ``$HERMES_HOME/policy/decisions.jsonl`` (rotated at 5 MB, one backup kept). A malformed policy
  file fails OPEN, unless enforce was ever active (see the marker below).
* enforce: ASK returns ``{"action": "approve"}`` (the engine's human approval gate; fails closed
  without a human, cron follows ``approvals.cron_mode``), BLOCK returns ``{"action": "block"}``.
  Every non-AUTO decision is logged. A malformed policy file or an internal error fails CLOSED.

Once enforce has been active for a home the plugin writes ``$HERMES_HOME/policy/.enforced``. With
that marker present, a missing root file or a broken policy file blocks every call instead of
switching the guard off. Only a human removes the marker (the policy BLOCKs agents from touching it).

No policy file and no marker means the plugin is inert (AUTO for everything, nothing logged).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import policy_engine as pe

logger = logging.getLogger(__name__)

PLUGIN_NAME = "synthwave-policy"
_CACHE = pe.PolicyCache()
_ERROR_LOGGED: Dict[str, Any] = {}


def _decision_log_path(home: Path) -> Path:
    return home / "policy" / "decisions.jsonl"


def _safe_log(home: Path, record: Dict[str, Any], policy: Optional[pe.Policy] = None) -> None:
    try:
        max_bytes = policy.max_bytes if policy else pe.DEFAULT_MAX_BYTES
        keep = policy.keep if policy else pe.DEFAULT_KEEP
        pe.append_jsonl(_decision_log_path(home), record, max_bytes=max_bytes, keep=keep)
    except Exception as exc:  # logging must never break a tool call
        logger.debug("synthwave-policy: decision log write failed: %s", exc)


def _base_record(home: Path, profile: str, tool_name: str, args: Any, kw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ts": pe.now_iso(),
        "profile": profile,
        "home": str(home),
        "tool": tool_name,
        "args": pe.summarize_args(tool_name, args),
        "session_id": kw.get("session_id") or "",
        "task_id": kw.get("task_id") or "",
        "tool_call_id": kw.get("tool_call_id") or "",
    }


def _block_message(decision: pe.Decision) -> str:
    rule = decision.rule
    desc = rule.description if rule else "policy"
    return (f"Blocked by the Synthwave autonomy policy (rule {rule.id if rule else 'unknown'}): {desc}. "
            "This action is not allowed for an agent. Do not retry or work around it; tell the user "
            "what you wanted to do so a human can do it or change the policy.")


def _ask_message(decision: pe.Decision) -> str:
    rule = decision.rule
    desc = rule.description if rule else "policy"
    return f"Synthwave autonomy policy rule {rule.id if rule else 'unknown'}: {desc}"


def _fail_closed_message(reason: str) -> str:
    return f"Blocked by the Synthwave autonomy policy: {reason}"


# --- browser page tracking (send-surface rules) ----------------------------------------------------

_PAGE_LOCK = threading.Lock()
_PAGE_URLS: "OrderedDict[str, str]" = OrderedDict()
_PAGE_MAX = 256
_SCREEN_TOOLS = ("browser_", "computer_use")


def _page_key(kw: Dict[str, Any]) -> str:
    return str(kw.get("task_id") or kw.get("session_id") or "")


def _remember_page(kw: Dict[str, Any], url: Any) -> None:
    key = _page_key(kw)
    if not key or not isinstance(url, str) or not url:
        return
    with _PAGE_LOCK:
        _PAGE_URLS[key] = url[:2048]
        _PAGE_URLS.move_to_end(key)
        while len(_PAGE_URLS) > _PAGE_MAX:
            _PAGE_URLS.popitem(last=False)


def _current_page(kw: Dict[str, Any]) -> str:
    key = _page_key(kw)
    with _PAGE_LOCK:
        return _PAGE_URLS.get(key, "") if key else ""


def _lease_held_by_human(home: Path) -> bool:
    """The Bot Screen lease (read only). A human holder, or a lease file the engine cannot read,
    means the engine itself refuses every agent screen action, so the policy adds nothing."""
    path = home / "bot-desktop" / "lease.json"
    try:
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True
    return not isinstance(data, dict) or data.get("holder") != "agent"


# --- Scout job detection ----------------------------------------------------------------------------

_JOBS_CACHE: Dict[str, Tuple[Tuple[float, int], Dict[str, Dict[str, Any]]]] = {}


def _cron_job_id(kw: Dict[str, Any]) -> str:
    task = str(kw.get("task_id") or "")
    if task.startswith("cron:"):
        return task.split(":")[1] if task.count(":") >= 1 else ""
    sid = str(kw.get("session_id") or "")
    if sid.startswith("cron_"):
        parts = sid[5:].rsplit("_", 2)
        return parts[0] if len(parts) == 3 else ""
    return ""


def _jobs_by_id(home: Path) -> Dict[str, Dict[str, Any]]:
    path = home / "cron" / "jobs.json"
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (st.st_mtime, st.st_size)
    hit = _JOBS_CACHE.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        table = {str(j.get("id")): j for j in jobs if isinstance(j, dict) and j.get("id")}
    except Exception:
        table = {}
    _JOBS_CACHE[str(path)] = (key, table)
    return table


def is_scout_run(home: Path, kw: Dict[str, Any]) -> bool:
    """True for a Scout cron run: the scheduler's HERMES_CRON_MANAGED_BY says scout, or the cron job
    behind this task is marked managed_by scout or runs the Scout collector script."""
    if os.environ.get("HERMES_CRON_MANAGED_BY", "").strip().lower() == "scout":
        return True
    job_id = _cron_job_id(kw)
    if not job_id:
        return False
    job = _jobs_by_id(home).get(job_id)
    if not job:
        return False
    return (str(job.get("managed_by") or "").lower() == "scout"
            or "scout_collect.py" in str(job.get("script") or ""))


# --- the hook ---------------------------------------------------------------------------------------

def _log_errors_once(home: Path, profile: str, tool_name: str, args: Any, kw: Dict[str, Any],
                     eff: "pe.Effective") -> None:
    key = "|".join(f"{p}:{e}" for p, e in eff.errors) + f"|{eff.fail_closed or ''}"
    if _ERROR_LOGGED.get(str(home)) == key:
        return
    _ERROR_LOGGED[str(home)] = key
    rec = _base_record(home, profile, tool_name, args, kw)
    first = eff.errors[0] if eff.errors else (eff.marker, Exception("root policy file missing"))
    rec.update({"decision": "ERROR", "mode": eff.mode, "rule": "policy-file-error",
                "outcome": "blocked" if eff.fail_closed else "ignored",
                "error": pe.redact(str(first[1]), 500), "policy": str(first[0])})
    _safe_log(home, rec)
    logger.warning("synthwave-policy: %s (mode %s)", rec["error"], eff.mode)


def check(tool_name: str, args: Any, home: Optional[Path] = None, profile: Optional[str] = None,
          **kw: Any) -> Optional[Dict[str, Any]]:
    """Core hook logic, separated from the engine signature for tests."""
    home = home or pe.current_home()
    profile = profile or pe.profile_for_home(home)
    eff = pe.load_effective(home, profile, _CACHE)
    if eff.errors or eff.fail_closed:
        _log_errors_once(home, profile, tool_name, args, kw, eff)
        if eff.fail_closed:
            return {"action": "block", "message": _fail_closed_message(eff.fail_closed)}
    else:
        _ERROR_LOGGED.pop(str(home), None)
    if eff.inert:
        return None
    sources = eff.enforcing_sources(profile)
    if sources:
        try:
            pe.write_enforced_marker(home, profile, sources)
        except Exception as exc:
            logger.debug("synthwave-policy: cannot write enforce marker: %s", exc)

    name = str(tool_name or "")
    screen_tool = name.lower().startswith(_SCREEN_TOOLS)
    skip_tags = ["bot-screen"] if screen_tool and _lease_held_by_human(home) else []
    page_url = _current_page(kw) if screen_tool else ""
    decision = eff.evaluate(name, args, profile, page_url=page_url, skip_tags=skip_tags)
    if is_scout_run(home, kw):
        sd = pe.scout_decision(name, eff.mode, profile)
        if sd is not None:
            decision = pe.combine([decision, sd])
            decision.mode = eff.mode
    if name == "browser_navigate" and decision.decision != "BLOCK" and isinstance(args, dict):
        _remember_page(kw, args.get("url"))
    if decision.decision == "AUTO":
        return None
    enforce = decision.mode == "enforce"
    if decision.decision == "BLOCK":
        outcome = "blocked" if enforce else "would_block"
    else:
        outcome = "asked" if enforce else "would_ask"
    rec = _base_record(home, profile, tool_name, args, kw)
    rec.update({
        "decision": decision.decision, "mode": decision.mode, "outcome": outcome,
        "rule": decision.rule_id, "rule_description": decision.rule.description if decision.rule else "",
        "matched": [r.id for r, _ in decision.matched],
        "policy": ", ".join(str(p) for p, _ in eff.policies),
    })
    if page_url:
        rec["page_url"] = pe.redact(page_url, 300)
    log_policy = eff.policies[0][1] if eff.policies else None
    _safe_log(home, rec, log_policy)
    if not enforce:
        return None
    if decision.decision == "BLOCK":
        return {"action": "block", "message": _block_message(decision)}
    return {"action": "approve", "message": _ask_message(decision),
            "rule_key": pe.approval_rule_key(decision.rule_id or "rule", tool_name, args)}


def _hook_ids(kw: Dict[str, Any]) -> Dict[str, Any]:
    return {k: kw.get(k) for k in ("session_id", "task_id", "tool_call_id")}


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **kw: Any) -> Optional[Dict[str, Any]]:
    try:
        return check(tool_name, args, **_hook_ids(kw))
    except Exception as exc:  # never let an internal bug decide silently
        logger.exception("synthwave-policy: internal error")
        mode = "observe"
        try:
            home = pe.current_home()
            profile = pe.profile_for_home(home)
            if pe.enforced_marker(home) is not None:
                mode = "enforce"
            for path in (pe.root_policy_path(), pe.profile_policy_path(home)):
                if path is not None and (pe.raw_mode_hint(path) == "enforce"
                                         or _CACHE.last_good_mode_for(path, profile) == "enforce"):
                    mode = "enforce"
        except Exception:
            mode = "enforce" if _CACHE.last_good else "observe"
        if mode == "enforce":
            return {"action": "block",
                    "message": f"Blocked by the Synthwave autonomy policy: internal error ({type(exc).__name__}). "
                               "The policy is in enforce mode and fails closed."}
        return None


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, **kw: Any) -> None:
    """Remember where a browser session landed (after redirects) for the send-surface rules."""
    try:
        if tool_name not in ("browser_navigate", "browser_back"):
            return
        data = json.loads(result) if isinstance(result, str) else result
        if isinstance(data, dict) and isinstance(data.get("url"), str):
            _remember_page(_hook_ids(kw), data["url"])
    except Exception:
        return


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
