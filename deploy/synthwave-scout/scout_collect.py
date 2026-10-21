#!/usr/bin/env python3
"""Scout collector: the read-only pre-run script of a bot's daily Scout cron job.

Runs under the Hermes cron scheduler for one profile (profile = HERMES_HOME). It gathers cheap,
already-available signals, drops everything the seen-ledger already knows, and prints either a
compact markdown digest for the agent or, when there is nothing new, the wake gate line
``{"wakeAgent": false}`` so the agent run is skipped silently.

Sources (all strictly read-only):
  a. this profile's other cron jobs: newest output since the last Scout run, plus failing jobs
  b. kanban tasks assigned to this profile: blocked, in review, stale, or available to pick up
  c. the bot's screen (Bot Desktop): a human holding it for > 8 h, the agent holding it with a
     pending takeover reason for > 2 h, an unreadable lease, or a recent launcher start failure
  d. web topics: NOT searched here (no credential-free search endpoint is available to a
     scrubbed cron script). When topics are configured and a topic sweep is due, the digest
     lists them and the agent searches with its own web_search tool.

Ledger feedback: the Scout agent has no terminal. It ends its answer with
``SCOUT-LEDGER: <hash8>=<status>,...`` and one ``SCOUT-WEB: <url> | <title> | <status>`` line per web
result. At the start of each run this script reads the previous answer of its own job from
``<HERMES_HOME>/cron/output/<job id>/`` and applies those lines. A [SILENT] or empty answer means
every item shown in that run is ignored; a failed or missing run leaves them 'new' so they are
shown again (at most ``scout_ledger.MAX_SHOWS`` times, then auto-ignored).

The only write is the Scout's own ledger (``<HERMES_HOME>/scout/seen.db`` or
``$SCOUT_LEDGER_DIR/seen.db``). No arguments are needed or accepted.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scout_ledger  # noqa: E402

WAKE_FALSE = json.dumps({"wakeAgent": False})
MAX_ITEMS = 40
MAX_CHARS = int(os.environ.get("SCOUT_MAX_CHARS", "6000"))
SUMMARY_CHARS = 400
STALE_DAYS = 3
FIRST_RUN_LOOKBACK_HOURS = 72
TOPIC_INTERVAL_HOURS = float(os.environ.get("SCOUT_TOPIC_INTERVAL_HOURS", "20"))
MAX_TOPICS = 8
RUNTIME_BUDGET_S = 50.0
OK_STATUSES = {"ok", "success", "silent", "skipped", None, ""}

# Lower = more important; within a rank newest first.
RANK = {"job_failure": 0, "screen": 1, "kanban_blocked": 2, "kanban_review": 3, "kanban_stale": 4,
        "kanban_opportunity": 5, "job_output": 6}
SCREEN_HUMAN_HOURS = 8
SCREEN_WAIT_HOURS = 2
SCREEN_LOG_HOURS = 24
NOTES_WAKE_HOURS = 20  # a run with only source-error notes wakes the agent at most about once a day


class ConfigError(Exception):
    pass


# --- environment ---------------------------------------------------------------------------

def resolve_context() -> dict:
    home = scout_ledger.hermes_home()
    profile = home.name if home.parent.name == "profiles" else "default"
    root = home.parent.parent if home.parent.name == "profiles" else home
    kanban = (os.environ.get("SCOUT_KANBAN_DB") or os.environ.get("HERMES_KANBAN_DB") or "").strip()
    if kanban:
        kanban_db = Path(kanban).expanduser()
    else:
        khome = os.environ.get("HERMES_KANBAN_HOME", "").strip()
        kanban_db = (Path(khome).expanduser() if khome else root) / "kanban.db"
    return {"home": home, "profile": profile, "root": root, "kanban_db": kanban_db,
            "ledger": scout_ledger.ledger_path(home),
            "self_job": os.environ.get("HERMES_CRON_JOB_ID", "").strip()}


# --- profile metadata ----------------------------------------------------------------------

def _load_yaml(text: str):
    try:
        import yaml  # the engine's interpreter ships PyYAML
    except ImportError:
        return _mini_yaml(text)
    return yaml.safe_load(text)


def _scalar(v: str):
    v = v.strip()
    if v in ("", "~", "null"):
        return None
    if v.lower() in ("true", "yes", "on"):
        return True
    if v.lower() in ("false", "no", "off"):
        return False
    if v.startswith("[") and v.endswith("]"):
        return [_scalar(x) for x in v[1:-1].split(",") if x.strip()]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def _mini_yaml(text: str):
    """Fallback for the block-map/list subset profile.yaml uses (no PyYAML available)."""
    root: dict = {}
    stack: list[tuple[int, object]] = [(-1, root)]
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for i, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if body.startswith("- "):
            if isinstance(parent, list):
                parent.append(_scalar(body[2:]))
            continue
        if ":" not in body:
            continue
        key, _, val = body.partition(":")
        key = key.strip().strip("'\"")
        if val.strip():
            if isinstance(parent, dict):
                parent[key] = _scalar(val)
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        is_list = nxt.strip().startswith("- ")
        child: object = [] if is_list else {}
        if isinstance(parent, dict):
            parent[key] = child
        # A block list may sit at its key's own indent; anchor it just below its first item.
        child_indent = (len(nxt) - len(nxt.lstrip())) - 0.5 if is_list else indent
        stack.append((child_indent, child))
    return root


def load_meta(home: Path) -> dict:
    # SCOUT_META_FILE: read-only dry runs against a real profile with a stand-in profile.yaml.
    override = os.environ.get("SCOUT_META_FILE", "").strip()
    path = Path(override).expanduser() if override else home / "profile.yaml"
    if not path.is_file():
        return {"title": home.name, "scout": {}}
    try:
        data = _load_yaml(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # broken YAML is a broken config
        raise ConfigError(f"profile.yaml is not readable YAML ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise ConfigError("profile.yaml is not a mapping")
    bots = ((data.get("ui_meta") or {}).get("hermes-bots") or {})
    if not isinstance(bots, dict):
        raise ConfigError("ui_meta.hermes-bots is not a mapping")
    scout = bots.get("scout") or {}
    if not isinstance(scout, dict):
        raise ConfigError("ui_meta.hermes-bots.scout is not a mapping")
    topics = scout.get("topics") or []
    if isinstance(topics, str):
        topics = [t for t in re.split(r"[,\n]", topics)]
    if not isinstance(topics, list):
        raise ConfigError("scout.topics must be a list")
    scout = dict(scout)
    scout["topics"] = [clean(t, 120) for t in topics if str(t or "").strip()][:MAX_TOPICS]
    return {
        "title": clean(bots.get("title") or home.name, 120),
        "role": clean(bots.get("role") or "", 200),
        "description": clean(bots.get("description") or data.get("description") or "", 300),
        "category": clean(bots.get("category") or "", 60),
        "scout": scout,
    }


# --- text hygiene --------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BOX = re.compile(r"[─-╿▀-▟⠀-⣿]")
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿\x00-\x08\x0b-\x1f\x7f]")
# Copied from synthwave-policy (policy_engine._SECRET_PATTERNS) plus AWS access keys and Telegram
# bot tokens. Kept in this repo on purpose: the Scout script must run without the plugin.
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)((?:password|passwd|pwd|token|secret|api[_-]?key|apikey|authorization|auth|"
                r"access[_-]?key|private[_-]?key|client[_-]?secret|x-api-key)\s*[:=]\s*)(['\"]?)[^\s'\",}]+"),
     r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(--(?:password|token|secret|api-key|key)[= ])\S+"), r"\1[REDACTED]"),
    (re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{8,}"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED]"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\b\d{6,12}:AA[A-Za-z0-9_-]{30,}"), "[REDACTED]"),  # Telegram bot token
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
    # The policy version also allows "/" here; the Scout drops it so file paths in job errors stay readable.
    (re.compile(r"\b[A-Za-z0-9+_-]{40,}={0,2}"), "[REDACTED_LONG]"),
]


def redact(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


_DASHES = re.compile("\\s*[\u2013\u2014]\\s*")


# Phrases the cron prompt scanner treats as injection; third-party text must never trip it
# (a tripped scan blocks the whole agent run), so they are neutralised before printing.
_INJECTION = re.compile(
    r"(?i)(ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions|"
    r"do\s+not\s+tell\s+the\s+user|system\s+prompt\s+override|"
    r"disregard\s+(?:your|all|any)\s+(?:instructions|rules|guidelines))")


def clean(text, limit: int) -> str:
    s = str(text or "")
    s = _ANSI.sub("", s)
    s = _INVISIBLE.sub(" ", s)
    s = _BOX.sub(" ", s)
    s = s.replace("```", "'''")
    s = redact(s)
    s = _DASHES.sub(", ", s)
    s = _INJECTION.sub("[filtered]", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: max(0, limit - 3)].rstrip() + "..."
    return s


_NOISE_LINE = re.compile(
    r"^\s*(\*\*(Job ID|Run Time|Mode|Status|Schedule|Model)\:?\*\*|#\s*Cron Job:|---+$|"
    r"[│┊╭╰]|Initializing agent|Warning: Unknown toolsets|Query:)")


def summarize_output(text: str) -> tuple[str, str]:
    """``(status, summary)`` from a cron output markdown document."""
    status = ""
    m = re.search(r"^\*\*Status:\*\*\s*(.+)$", text, re.M)
    if m:
        status = m.group(1).strip()
    body = text.rpartition("## Response")[2] if "## Response" in text else text
    kept = [ln for ln in body.splitlines() if ln.strip() and not _NOISE_LINE.match(ln)]
    return status, clean(" ".join(kept), SUMMARY_CHARS)


def iso(ts: float | int | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _parse_iso(value) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --- sources -------------------------------------------------------------------------------

def load_jobs(ctx: dict) -> list[dict]:
    jobs_file = ctx["home"] / "cron" / "jobs.json"
    if not jobs_file.is_file():
        return []
    data = json.loads(jobs_file.read_text(encoding="utf-8-sig"))
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []


def is_scout_job(job: dict, ctx: dict) -> bool:
    jid = str(job.get("id") or "")
    return bool(jid) and (jid == ctx["self_job"] or job.get("managed_by") == "scout"
                          or "scout_collect.py" in str(job.get("script") or ""))


def _streak_start(led, jid: str, job: dict, failing: bool) -> str:
    """When the current failure streak started, so a job that recovers and fails again is new."""
    key = f"fail_start:{jid}"
    if not failing:
        if led.get_meta(key) is not None:
            led.del_meta(key)
        return ""
    stored = led.get_meta(key)
    try:
        streak = int(job.get("failure_streak") or 0)
    except (TypeError, ValueError):
        streak = 0
    if stored is None or streak == 1:
        start = str(job.get("last_run_at") or "") or iso(time.time())
        if stored != start:
            led.set_meta(key, start)
        return start
    return stored


def collect_jobs(ctx: dict, since: float, led=None) -> list[dict]:
    out_root = ctx["home"] / "cron" / "output"
    items: list[dict] = []
    for job in load_jobs(ctx):
        jid = str(job.get("id") or "")
        if not jid or is_scout_job(job, ctx):
            continue
        name = clean(job.get("name") or jid, 100)
        enabled = job.get("enabled", True) is not False
        status = job.get("last_status")
        failing = enabled and status not in OK_STATUSES
        start = _streak_start(led, jid, job, failing) if led is not None else ""
        if failing:
            err = clean(job.get("last_error") or "", 160)
            sig = re.sub(r"\d+", "#", err)[:80]
            streak = job.get("failure_streak")
            summary = f"last_status={status}"
            if streak:
                summary += f", failure_streak={streak}"
            if err:
                summary += f": {err}"
            items.append({"source": "job_failure", "id_or_url": f"{jid}:fail:{status}:{sig}:{start}",
                          "title": f"Job faalt: {name}", "summary": clean(summary, SUMMARY_CHARS),
                          "ts": _parse_iso(job.get("last_run_at")) or time.time()})
        out_dir = out_root / jid
        if not out_dir.is_dir():
            continue
        newest = None
        with os.scandir(out_dir) as it:
            for entry in it:
                if entry.name.endswith(".md") and entry.is_file():
                    mt = entry.stat().st_mtime
                    if newest is None or mt > newest[0]:
                        newest = (mt, entry.path, entry.name)
        if not newest or newest[0] <= since:
            continue
        with open(newest[1], "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(200_000)
        out_status, summary = summarize_output(text)
        if out_status.lower().startswith("silent") or not summary:
            continue
        items.append({"source": "job_output", "id_or_url": f"{jid}:{newest[2]}",
                      "title": f"Output: {name}" + (f" ({clean(out_status, 40)})" if out_status else ""),
                      "summary": summary, "ts": newest[0]})
    return items


# Lines the Bot Desktop launcher (tools/bot_desktop/launcher.sh, runtime.py) writes when a start fails.
# launcher.log also carries harmless dbus "failed" chatter, so only these count.
_LAUNCH_FAILURE = re.compile(
    r"(Xvnc exited during startup|Xvnc did not become ready|Fatal server error|did not publish its display|"
    r"launcher exited with|cannot open display|Traceback \(most recent call last\))")


def _pid_alive(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return False
    return Path(f"/proc/{pid}").exists()


def collect_screen(ctx: dict, now: float) -> list[dict]:
    """The bot's screen (read only: never touches the lease or the desktop)."""
    sd = ctx["home"] / "bot-desktop"
    if not sd.is_dir():
        return []
    items: list[dict] = []
    lease_file = sd / "lease.json"
    if lease_file.is_file():
        try:
            lease = json.loads(lease_file.read_text(encoding="utf-8"))
            if not isinstance(lease, dict) or lease.get("holder") not in ("agent", "human"):
                raise ValueError("unexpected lease content")
        except (OSError, ValueError) as exc:
            mt = int(lease_file.stat().st_mtime)
            items.append({"source": "screen", "id_or_url": f"screen:lease-unreadable:{mt}",
                          "title": "Bot Screen: lease onleesbaar",
                          "summary": clean(f"lease.json is niet te lezen ({type(exc).__name__}); de engine behandelt "
                                           "het scherm dan als door een mens bezet en de bot kan niets op het scherm.",
                                           SUMMARY_CHARS), "ts": mt})
            lease = None
        if isinstance(lease, dict):
            try:
                since = float(lease.get("since") or 0)
            except (TypeError, ValueError):
                since = 0.0
            reason = clean(lease.get("reason") or "", 200)
            held_h = (now - since) / 3600 if since else 0
            if lease.get("holder") == "human" and held_h > SCREEN_HUMAN_HOURS:
                items.append({"source": "screen", "id_or_url": f"screen:human:{int(since)}",
                              "title": "Bot Screen: al lang door een mens overgenomen",
                              "summary": clean(f"Een mens houdt het scherm sinds {iso(since)} ({held_h:.0f} uur)."
                                               + (f" Reden: {reason}" if reason else "")
                                               + " Geef het terug als het klaar is.", SUMMARY_CHARS),
                              "ts": since})
            elif lease.get("holder") == "agent" and reason and held_h > SCREEN_WAIT_HOURS:
                items.append({"source": "screen", "id_or_url": f"screen:waiting:{int(since)}",
                              "title": "Bot Screen: wacht op overname door een mens",
                              "summary": clean(f"De bot vroeg sinds {iso(since)} om hulp: {reason}", SUMMARY_CHARS),
                              "ts": since})
    log = sd / "launcher.log"
    if log.is_file():
        mt = log.stat().st_mtime
        if now - mt < SCREEN_LOG_HOURS * 3600 and not _pid_alive(sd / "launcher.pid"):
            with open(log, "rb") as fh:
                fh.seek(max(0, log.stat().st_size - 8192))
                tail = fh.read().decode("utf-8", "replace")
            hits = [ln for ln in tail.splitlines() if _LAUNCH_FAILURE.search(ln)]
            if hits:
                items.append({"source": "screen", "id_or_url": f"screen:launch-failed:{int(mt)}",
                              "title": "Bot Screen: starten mislukt",
                              "summary": clean(f"launcher.log ({iso(mt)}): {hits[-1]}", SUMMARY_CHARS), "ts": mt})
    return items


def collect_kanban(ctx: dict, now: float) -> list[dict]:
    db = ctx["kanban_db"]
    if not db.is_file():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only = 1")
        has_events = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_events'").fetchone()
        last_evt = ("(SELECT MAX(e.created_at) FROM task_events e WHERE e.task_id = t.id)"
                    if has_events else "NULL")
        rows = conn.execute(
            f"SELECT t.id, t.title, t.status, t.body, t.last_failure_error, t.created_at, "
            f"t.started_at, t.last_heartbeat_at, {last_evt} "
            f"FROM tasks t WHERE t.assignee = ? AND t.status NOT IN ('done', 'archived')",
            (ctx["profile"],)).fetchall()
    finally:
        conn.close()
    items: list[dict] = []
    stale_cut = now - STALE_DAYS * 86400
    for tid, title, status, body, fail, created, started, beat, evt in rows:
        last = max(int(x) for x in (created, started, beat, evt) if x) if any(
            (created, started, beat, evt)) else 0
        detail = clean(fail or body or "", SUMMARY_CHARS - 60)
        if status == "blocked":
            src, label = "kanban_blocked", "Geblokkeerd"
        elif status == "review":
            src, label = "kanban_review", "Wacht op review"
        elif last and last < stale_cut:
            src, label = "kanban_stale", f"Stil sinds {iso(last)[:10]}"
        elif status in ("todo", "ready", "triage"):
            src, label = "kanban_opportunity", "Voorstel om op te pakken"
        else:
            continue
        items.append({"source": src, "id_or_url": f"{tid}:{status}:{last}",
                      "title": f"Taak {tid}: {clean(title, 100)}",
                      "summary": clean(f"{label} (status {status}). {detail}", SUMMARY_CHARS),
                      "ts": last or now})
    return items


# --- ledger feedback from the previous Scout answer ---------------------------------------------

LEDGER_LINE = re.compile(r"^[ \t>*`_-]{0,6}SCOUT-LEDGER:[ \t]*(?P<body>[^\n]{0,4000})", re.M)
LEDGER_ENTRY = re.compile(r"^(?P<hash>[0-9a-f]{8,12})=(?P<status>ignored|saved|attached|notified)$")
WEB_LINE = re.compile(r"^[ \t>*`_-]{0,6}SCOUT-WEB:[ \t]*(?P<url>https?://[^\s|]{1,2000})[ \t]*\|[ \t]*"
                      r"(?P<title>[^|\n]{0,300}?)[ \t]*(?:\|[ \t]*(?P<status>ignored|saved|notified)[ \t]*)?`?$", re.M)
MAX_LEDGER_ENTRIES = 100
MAX_WEB_LINES = 30
MAX_FEEDBACK_FILES = 5
_SILENT_MARKERS = {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}
_RESPONSE_FRAME = re.compile(r"(?m)^\*\*Response Characters:\*\* (\d+)\n## Response\n\n")


def own_job_id(ctx: dict) -> str:
    if ctx["self_job"]:
        return ctx["self_job"]
    try:
        for job in load_jobs(ctx):
            if job.get("managed_by") == "scout" or "scout_collect.py" in str(job.get("script") or ""):
                return str(job.get("id") or "")
    except (OSError, ValueError):
        pass
    return ""


def classify_run_doc(text: str) -> tuple[str, str]:
    """``(kind, answer)`` for one stored run of the Scout job.

    kind: ``answer`` (agent replied), ``silent`` (agent replied [SILENT] or nothing),
    ``gate`` (the collector said wakeAgent=false, no agent ran) or ``failed``.
    """
    head = text[:4000]
    if re.search(r"^# Cron Job: .*\(FAILED\)\s*$", head, re.M) or re.search(
            r"^\*\*Status:\*\* (script failed|monitor source failed|BLOCKED)", head, re.M):
        return "failed", ""
    if re.search(r"^\*\*Status:\*\* (silent|no_change)", head, re.M):
        return "gate", ""
    frames = list(_RESPONSE_FRAME.finditer(text))
    if frames:
        answer = text[frames[-1].end():]
    elif "## Response" in text:
        answer = text.rpartition("## Response")[2]
    else:
        return "failed", ""
    answer = answer.strip()
    if answer.startswith("[CRON_FAILURE]"):
        return "failed", ""
    if not answer or answer == "(No response generated)":
        return "silent", ""
    lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
    if answer.upper().startswith("[SILENT]") or any(
            x.upper() in _SILENT_MARKERS for x in (answer, lines[0], lines[-1])):
        return "silent", answer
    return "answer", answer


def parse_feedback(answer: str) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Strictly parse SCOUT-LEDGER and SCOUT-WEB lines. Malformed entries are dropped."""
    entries: list[tuple[str, str]] = []
    for m in LEDGER_LINE.finditer(answer):
        for part in re.split(r"[,;\s]+", m.group("body").strip().strip("`")):
            e = LEDGER_ENTRY.match(part.strip().lower())
            if e and len(entries) < MAX_LEDGER_ENTRIES:
                entries.append((e.group("hash"), e.group("status")))
    web: list[tuple[str, str, str]] = []
    for m in WEB_LINE.finditer(answer):
        if len(web) >= MAX_WEB_LINES:
            break
        web.append((m.group("url"), clean(m.group("title"), 200), m.group("status") or "notified"))
    return entries, web


def apply_feedback(ctx: dict, led, now: float) -> list[str]:
    """Apply the previous Scout answer(s) to the ledger. Returns notes for the digest."""
    notes: list[str] = []
    pending_raw = led.get_meta("pending_run")
    try:
        pending = json.loads(pending_raw) if pending_raw else None
    except ValueError:
        pending = None
    job_id = own_job_id(ctx)
    since = float(led.get_meta("feedback_mark") or 0)
    if pending:
        since = min(since or float(pending.get("at", 0)), float(pending.get("at", 0)))
    docs: list[tuple[float, str]] = []
    out_dir = ctx["home"] / "cron" / "output" / job_id if job_id else None
    if out_dir is not None and out_dir.is_dir():
        with os.scandir(out_dir) as it:
            for entry in it:
                if entry.name.endswith(".md") and entry.is_file():
                    mt = entry.stat().st_mtime
                    if mt > since:
                        docs.append((mt, entry.path))
    docs.sort()
    docs = docs[-MAX_FEEDBACK_FILES:]
    run_kind = None
    for mt, path in docs:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            kind, answer = classify_run_doc(fh.read(400_000))
        if kind == "gate":
            continue
        if run_kind is None and pending and mt > float(pending.get("at", 0)):
            run_kind = kind
        if kind == "answer":
            entries, web = parse_feedback(answer)
            applied = 0
            for short, status in entries:
                try:
                    h = led.resolve(short)
                except (ValueError, LookupError):
                    continue  # unknown or ambiguous hash: ignored on purpose
                led.set_status(h, status, now=int(now))
                applied += 1
            for url, title, status in web:
                led.upsert_web(url, title or url, status, now=int(now))
    if docs:
        led.set_meta("feedback_mark", str(docs[-1][0]))
    if pending:
        hashes = [h for h in pending.get("hashes", []) if isinstance(h, str)]
        if run_kind == "silent":
            for h in hashes:
                led.set_status(h, "ignored", now=int(now), only_from_new=True)
        elif run_kind in (None, "failed") and hashes:
            notes.append("vorige Scout-run mislukte of gaf geen antwoord; niet geclassificeerde items komen terug")
        led.del_meta("pending_run")
    return notes


# --- rendering -----------------------------------------------------------------------------

def render(meta: dict, ctx: dict, items: list[dict], notes: list[str], topics: list[str],
           budget: int | None = None, known_web: list[tuple[str, str]] | None = None) -> tuple[str, list[dict]]:
    budget = MAX_CHARS if budget is None else budget
    head = [f"# Scout digest: {meta['title']} (profiel {ctx['profile']})"]
    who = " | ".join(x for x in (meta.get("role"), meta.get("category")) if x)
    if who:
        head.append(f"Rol: {who}")
    if meta.get("description"):
        head.append(f"Over deze bot: {meta['description']}")
    head.append(f"{len(items)} items hieronder (een item zonder uitkomst komt bij de volgende run terug).")
    if topics:
        head.append("")
        head.append("## Webonderwerpen (zoek zelf met web_search, max 5 resultaten per onderwerp)")
        head.extend(f"- {t}" for t in topics)
        if known_web:
            head.append("Al bekend, sla over:")
            head.extend(f"- {clean(u, 200)}" for u, _ in known_web)
    if notes:
        head.append("")
        head.append("## Bronnotities")
        head.extend(f"- {n}" for n in notes)
    text = "\n".join(head)
    shown: list[dict] = []
    if items:
        text += "\n\n## Items"
    for it in items[:MAX_ITEMS]:
        again = f" (opnieuw, {it['shown_count']}x getoond)" if it.get("shown_count") else ""
        block = (f"\n- [{it['hash'][:scout_ledger.SHORT_HASH]}] ({it['source']}) {it['title']}{again}"
                 f" | {iso(it['ts'])}\n  {it['summary']}")
        if len(text) + len(block) + 160 > budget and shown:
            break
        text += block
        shown.append(it)
    rest = len(items) - len(shown)
    if rest > 0:
        text += f"\n\n({rest} items niet getoond; die komen bij de volgende run terug.)"
    text += ("\n\nScout klaar: sluit je antwoord af met SCOUT-LEDGER: <hash>=<uitkomst>,... voor elk item"
             " hierboven en een SCOUT-WEB regel per webresultaat.")
    return text, shown


# --- main ----------------------------------------------------------------------------------

def run() -> int:
    started = time.monotonic()
    now = time.time()
    ctx = resolve_context()
    try:
        meta = load_meta(ctx["home"])
    except ConfigError as exc:
        print(f"Scout config error for profile {ctx['profile']}: {exc}", file=sys.stderr)
        return 2
    scout = meta["scout"]
    if not scout.get("enabled"):
        print(WAKE_FALSE)
        return 0

    led = scout_ledger.Ledger(ctx["ledger"])
    try:
        led.expire(now=int(now))
        notes: list[str] = []
        try:
            notes.extend(apply_feedback(ctx, led, now))
        except Exception as exc:  # feedback must never stop the digest
            notes.append(f"ledger-terugkoppeling: niet gelezen ({type(exc).__name__}: {clean(exc, 120)})")
        led.auto_ignore(now=int(now))
        # Job-output watermark: outputs newer than this are candidates. It only advances past
        # what was actually shown, and never when the jobs source failed or ran out of time.
        watermark = float(led.get_meta("jobs_watermark") or 0) or None
        since = watermark if watermark else now - FIRST_RUN_LOOKBACK_HOURS * 3600

        candidates: list[dict] = []
        jobs_ok = False
        for label, fn in (("cron-jobs", lambda: collect_jobs(ctx, since, led)),
                          ("kanban", lambda: collect_kanban(ctx, now)),
                          ("bot-screen", lambda: collect_screen(ctx, now))):
            if time.monotonic() - started > RUNTIME_BUDGET_S:
                notes.append(f"{label}: overgeslagen (tijdbudget op)")
                continue
            try:
                found = fn()
                candidates.extend(found)
                if label == "cron-jobs":
                    jobs_ok = time.monotonic() - started <= RUNTIME_BUDGET_S
            except Exception as exc:
                notes.append(f"{label}: niet gelezen ({type(exc).__name__}: {clean(exc, 120)})")

        fresh: list[dict] = []
        seen_batch: set[str] = set()
        for it in candidates:
            it["hash"] = scout_ledger.item_hash(it["source"], it["id_or_url"], it["title"])
            status = led.known(it["hash"])
            if it["hash"] in seen_batch or (status and status != "new"):
                continue
            seen_batch.add(it["hash"])
            fresh.append(it)
        # Rows shown before that nobody classified come back (up to MAX_SHOWS times).
        for row in led.unclassified():
            if row["hash"] not in seen_batch:
                seen_batch.add(row["hash"])
                fresh.append(row)
        for it in fresh:
            it.setdefault("shown_count", 0)
        fresh.sort(key=lambda i: (RANK.get(i["source"], 9), -float(i["ts"] or 0)))

        topics = scout.get("topics") or []
        last_topics = float(led.get_meta("last_topic_sweep") or 0)
        topics_due = bool(topics) and (now - last_topics) >= TOPIC_INTERVAL_HOURS * 3600
        last_notes_wake = float(led.get_meta("last_notes_wake") or 0)
        notes_due = bool(notes) and (now - last_notes_wake) >= NOTES_WAKE_HOURS * 3600

        if not fresh and not topics_due and not notes_due:
            if jobs_ok:
                led.set_meta("jobs_watermark", str(now))
            led.set_meta("last_run", str(now))
            print(WAKE_FALSE)
            return 0

        known_web = led.recent_web(now=int(now)) if topics_due else []
        text, shown = render(meta, ctx, fresh, notes, topics if topics_due else [], known_web=known_web)
        for it in shown:
            led.record_shown(it["hash"], it["source"], it["title"], it.get("id_or_url", ""), it["summary"],
                             float(it["ts"] or now), now=int(now))
        led.set_meta("pending_run", json.dumps({"at": now, "hashes": [it["hash"] for it in shown]}))
        if topics_due:
            led.set_meta("last_topic_sweep", str(now))
        if notes and not fresh and not topics_due:
            led.set_meta("last_notes_wake", str(now))
        if jobs_ok:
            shown_ids = {id(it) for it in shown}
            unshown = [float(it["ts"]) for it in fresh
                       if id(it) not in shown_ids and it["source"] == "job_output" and not it.get("shown_count")]
            led.set_meta("jobs_watermark", str(max(since, min(unshown) - 1) if unshown else now))
        led.set_meta("last_run", str(now))
        text += f"\n(Verzameld in {time.monotonic() - started:.1f}s.)"
        print(text)
        return 0
    finally:
        led.close()


if __name__ == "__main__":
    sys.exit(run())
