#!/usr/bin/env python3
"""Scout seen-ledger: a tiny per-profile sqlite store of items the Scout already surfaced.

Location: ``$SCOUT_LEDGER_DIR/seen.db`` when set, else ``<HERMES_HOME>/scout/seen.db``.
HERMES_HOME comes from the environment; when it is missing it is derived from this file's
location (installed at ``<HERMES_HOME>/scripts/scout/``), and finally ``~/.hermes``.

The Scout agent never runs this file: it has no terminal. It reports its decisions in its own
answer (``SCOUT-LEDGER:`` / ``SCOUT-WEB:`` lines) and the collector applies them at the start of
the next run. This CLI is for operators (stdlib only, safe to run with any python3):

    scout_ledger.py mark <hash> <status>       status: new|ignored|saved|attached|notified
    scout_ledger.py add <source> <url_or_id> <title>   record an item (e.g. a web hit); prints
                                               "<hash> new" or "<hash> known <status>"
    scout_ledger.py list [status] [limit]      show recent rows
    scout_ledger.py expire                     drop rows older than 90 days

A hash may be given as a unique prefix of at least 8 characters (the digest prints 8).
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

STATUSES = ("new", "ignored", "saved", "attached", "notified")
EXPIRE_DAYS = 90
SHORT_HASH = 8
MAX_SHOWS = 3  # a row nobody classified is shown at most this often, then auto-ignored

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS seen (
    hash       TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    title      TEXT,
    first_seen INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'new'
               CHECK (status IN ({", ".join(repr(s) for s in STATUSES)})),
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS seen_first_seen ON seen(first_seen);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first release (older ledgers are migrated in place).
_EXTRA_COLUMNS = (
    ("ref", "TEXT"),                              # id or URL the hash was made from
    ("summary", "TEXT"),                          # last digest text, to re-show an unclassified row
    ("item_ts", "REAL"),                          # when the item happened
    ("shown_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_shown", "INTEGER"),
)


# --- paths -------------------------------------------------------------------------------

def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    # <HERMES_HOME>/scripts/scout/scout_ledger.py
    if here.name == "scout" and here.parent.name == "scripts":
        return here.parent.parent
    return Path.home() / ".hermes"


def ledger_path(home: Path | None = None) -> Path:
    override = os.environ.get("SCOUT_LEDGER_DIR", "").strip()
    base = Path(override).expanduser() if override else (home or hermes_home()) / "scout"
    return base / "seen.db"


# --- hashing -----------------------------------------------------------------------------

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$)", re.I)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return urlunsplit((parts.scheme.lower() or "https", host, path, urlencode(sorted(query)), ""))


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def item_hash(source: str, id_or_url: str, title: str = "") -> str:
    ref = str(id_or_url or "").strip()
    if re.match(r"^https?://", ref, re.I):
        key = "url|" + normalize_url(ref)
    else:
        key = "|".join((_norm_text(source), _norm_text(ref), _norm_text(title)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# --- ledger ------------------------------------------------------------------------------

class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.conn = sqlite3.connect(str(self.path), timeout=10)
        self.conn.executescript(_SCHEMA)
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(seen)")}
        for name, decl in _EXTRA_COLUMNS:
            if name not in have:
                self.conn.execute(f"ALTER TABLE seen ADD COLUMN {name} {decl}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        self.conn.commit()

    # rows
    def known(self, h: str) -> str | None:
        row = self.conn.execute("SELECT status FROM seen WHERE hash = ?", (h,)).fetchone()
        return row[0] if row else None

    def insert_new(self, h: str, source: str, title: str, now: int | None = None) -> bool:
        now = int(now if now is not None else time.time())
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO seen(hash, source, title, first_seen, status, updated_at) "
            "VALUES (?, ?, ?, ?, 'new', ?)", (h, source, (title or "")[:300], now, now))
        self.conn.commit()
        return cur.rowcount == 1

    def del_meta(self, key: str) -> None:
        self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        self.conn.commit()

    def record_shown(self, h: str, source: str, title: str, ref: str, summary: str, item_ts: float,
                     now: int | None = None) -> int:
        """The digest showed this item: insert it as 'new' or bump its shown_count. Returns the count."""
        now = int(now if now is not None else time.time())
        self.conn.execute(
            "INSERT INTO seen(hash, source, title, first_seen, status, updated_at, ref, summary, item_ts,"
            " shown_count, last_shown) VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(hash) DO UPDATE SET shown_count = shown_count + 1, last_shown = excluded.last_shown,"
            " summary = excluded.summary, title = excluded.title",
            (h, source, (title or "")[:300], now, now, (ref or "")[:2048], (summary or "")[:1000],
             float(item_ts or now), now))
        self.conn.commit()
        row = self.conn.execute("SELECT shown_count FROM seen WHERE hash = ?", (h,)).fetchone()
        return int(row[0]) if row else 0

    def unclassified(self, limit: int = 100) -> list[dict]:
        """Rows shown before that nobody classified yet (status 'new'), oldest first."""
        rows = self.conn.execute(
            "SELECT hash, source, title, ref, summary, item_ts, shown_count FROM seen "
            "WHERE status = 'new' AND shown_count > 0 ORDER BY first_seen LIMIT ?", (int(limit),)).fetchall()
        return [{"hash": h, "source": src, "title": t or "", "id_or_url": ref or "", "summary": summ or "",
                 "ts": ts or 0, "shown_count": n} for h, src, t, ref, summ, ts, n in rows]

    def auto_ignore(self, max_shows: int = MAX_SHOWS, now: int | None = None) -> int:
        cur = self.conn.execute(
            "UPDATE seen SET status = 'ignored', updated_at = ? WHERE status = 'new' AND shown_count >= ?",
            (int(now if now is not None else time.time()), int(max_shows)))
        self.conn.commit()
        return cur.rowcount

    def set_status(self, h: str, status: str, now: int | None = None, only_from_new: bool = False) -> bool:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        sql = "UPDATE seen SET status = ?, updated_at = ? WHERE hash = ?"
        if only_from_new:
            sql += " AND status = 'new'"
        cur = self.conn.execute(sql, (status, int(now if now is not None else time.time()), h))
        self.conn.commit()
        return cur.rowcount == 1

    def upsert_web(self, url: str, title: str, status: str, now: int | None = None) -> str:
        """A web result the agent reported: record it with its status (known rows get the new status)."""
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        now = int(now if now is not None else time.time())
        h = item_hash("web", url, title)
        self.conn.execute(
            "INSERT INTO seen(hash, source, title, first_seen, status, updated_at, ref, item_ts) "
            "VALUES (?, 'web', ?, ?, ?, ?, ?, ?) ON CONFLICT(hash) DO UPDATE SET status = excluded.status,"
            " updated_at = excluded.updated_at", (h, (title or "")[:300], now, status, now, url[:2048], now))
        self.conn.commit()
        return h

    def recent_web(self, days: int = 30, limit: int = 20, now: int | None = None) -> list[tuple[str, str]]:
        cutoff = int(now if now is not None else time.time()) - days * 86400
        return self.conn.execute(
            "SELECT ref, title FROM seen WHERE source = 'web' AND ref IS NOT NULL AND ref != '' "
            "AND first_seen >= ? ORDER BY first_seen DESC LIMIT ?", (cutoff, int(limit))).fetchall()

    def resolve(self, prefix: str) -> str:
        prefix = prefix.strip().lower()
        if len(prefix) < 8 or not re.fullmatch(r"[0-9a-f]+", prefix):
            raise ValueError("hash must be at least 8 hex characters")
        rows = self.conn.execute(
            "SELECT hash FROM seen WHERE hash LIKE ? LIMIT 2", (prefix + "%",)).fetchall()
        if not rows:
            raise LookupError(f"unknown hash {prefix}")
        if len(rows) > 1:
            raise LookupError(f"ambiguous hash prefix {prefix}")
        return rows[0][0]

    def mark(self, prefix: str, status: str, now: int | None = None) -> str:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        h = self.resolve(prefix)
        self.conn.execute("UPDATE seen SET status = ?, updated_at = ? WHERE hash = ?",
                          (status, int(now if now is not None else time.time()), h))
        self.conn.commit()
        return h

    def expire(self, days: int = EXPIRE_DAYS, now: int | None = None) -> int:
        cutoff = int(now if now is not None else time.time()) - days * 86400
        cur = self.conn.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def rows(self, status: str | None = None, limit: int = 20) -> list[tuple]:
        sql = "SELECT hash, source, status, first_seen, title FROM seen"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY first_seen DESC LIMIT ?"
        return self.conn.execute(sql, args + (int(limit),)).fetchall()


# --- CLI ---------------------------------------------------------------------------------

def _usage() -> int:
    print(__doc__.split("CLI", 1)[1].split("A hash may", 1)[0].strip(), file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _usage()
    cmd, rest = args[0], args[1:]
    try:
        with Ledger(ledger_path()) as led:
            if cmd == "mark" and len(rest) == 2:
                h = led.mark(rest[0], rest[1])
                print(f"{h[:SHORT_HASH]} {rest[1]}")
            elif cmd == "add" and len(rest) >= 2:
                source, ref = rest[0], rest[1]
                title = " ".join(rest[2:])
                h = item_hash(source, ref, title)
                status = led.known(h)
                if status:
                    print(f"{h[:SHORT_HASH]} known {status}")
                else:
                    led.insert_new(h, source, title or ref)
                    led.conn.execute("UPDATE seen SET ref = ? WHERE hash = ?", (ref[:2048], h))
                    led.conn.commit()
                    print(f"{h[:SHORT_HASH]} new")
            elif cmd == "list":
                status = rest[0] if rest and rest[0] in STATUSES else None
                limit = int(rest[-1]) if rest and rest[-1].isdigit() else 20
                for h, source, st, first_seen, title in led.rows(status, limit):
                    day = time.strftime("%Y-%m-%d", time.localtime(first_seen))
                    print(f"{h[:SHORT_HASH]}  {st:<8}  {day}  {source:<10}  {title}")
            elif cmd == "expire":
                print(f"expired {led.expire()}")
            else:
                return _usage()
    except (ValueError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
