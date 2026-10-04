"""Quarantine only detached legacy FTS shadows; canonical rows are never edited.

Run offline after a consistent backup. SessionDB initialization then rebuilds
derived indexes from canonical messages using its normal admission controls.
"""
import sqlite3
from pathlib import Path

BASES = ("messages_fts", "messages_fts_trigram")
COLUMNS = {"data": ("id", "block"), "idx": ("segid", "term", "pgno"),
           "content": ("id", "c0"), "docsize": ("id", "sz"), "config": ("k", "v")}

def quarantine_orphaned_fts(conn):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        return _quarantine_orphaned_fts(conn)

def _quarantine_orphaned_fts(conn):
    plans = []
    for base in BASES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (base,)).fetchone():
            continue
        shadows = []
        for suffix, expected in COLUMNS.items():
            name = base + "_" + suffix
            row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if row is None:
                continue
            if row[0] != "table":
                raise ValueError("Unexpected orphan object type")
            columns = tuple(r[1] for r in conn.execute(f'PRAGMA table_info("{name}")'))
            if columns != expected:
                raise ValueError("Unexpected orphan shadow layout")
            target = "orphan_backup_" + name
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (target,)).fetchone():
                raise ValueError("Quarantine destination already exists")
            shadows.append((name, target))
        if shadows and len(shadows) != len(COLUMNS):
            raise ValueError("Incomplete legacy shadow set; manual diagnosis required")
        if shadows:
            plans.append((base, shadows))
    with conn:
        for base, shadows in plans:
            for suffix in ("insert", "update", "delete"):
                name = base + "_" + suffix
                row = conn.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
                if row and (row[0] != "trigger" or base not in (row[1] or "")):
                    raise ValueError("Unexpected FTS trigger")
                conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
        for base, shadows in plans:
            for name, target in shadows:
                conn.execute(f'ALTER TABLE "{name}" RENAME TO "{target}"')
    return [base for base, _ in plans]

def main():
    import argparse, json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args()
    if not args.db.is_file() or not args.backup.is_file() or args.db.resolve() == args.backup.resolve():
        parser.error("Existing database and distinct consistent backup required")
    conn = sqlite3.connect(args.db, timeout=10)
    try:
        print(json.dumps({"quarantined": quarantine_orphaned_fts(conn)}))
    finally:
        conn.close()
if __name__ == "__main__":
    main()
