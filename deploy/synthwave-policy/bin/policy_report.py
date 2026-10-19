#!/usr/bin/env python3
"""Summarize synthwave-policy decisions.jsonl files.

    policy_report.py [FILE ...] [--all] [--last N] [--since YYYY-MM-DD] [--json]

Without FILE it reads $HERMES_HOME/policy/decisions.jsonl (or ~/.hermes). With --all it reads the root
log plus every ~/.hermes/profiles/<name>/policy/decisions.jsonl. Rotated files (.1) are included.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "synthwave-policy", HERE):
    if (candidate / "policy_engine.py").exists():
        sys.path.insert(0, str(candidate))
        break

import policy_engine as pe  # noqa: E402


def default_files(all_profiles: bool) -> List[Path]:
    home_env = os.environ.get("HERMES_HOME", "").strip()
    home = Path(home_env).expanduser() if home_env else pe.root_home()
    files = [home / "policy" / "decisions.jsonl"]
    if all_profiles:
        root = pe.root_home()
        files = [root / "policy" / "decisions.jsonl"]
        profiles = root / "profiles"
        if profiles.is_dir():
            files += sorted(p / "policy" / "decisions.jsonl" for p in profiles.iterdir() if p.is_dir())
    return files


def read_records(files: Iterable[Path]) -> List[Dict]:
    records: List[Dict] = []
    for f in files:
        for part in (f.with_name(f.name + ".1"), f):
            if not part.exists():
                continue
            with part.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        records.append(rec)
    records.sort(key=lambda r: str(r.get("ts", "")))
    return records


def short_args(rec: Dict) -> str:
    a = rec.get("args") or {}
    for k in ("command", "path", "file_path", "target", "project_id", "to"):
        if a.get(k):
            v = str(a[k]).replace("\n", " ")
            return v[:90] + ("..." if len(v) > 90 else "")
    return ",".join(a.get("keys", [])[:6])


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Summarize synthwave-policy decisions.")
    ap.add_argument("files", nargs="*", help="decisions.jsonl files")
    ap.add_argument("--all", action="store_true", help="root plus every profile")
    ap.add_argument("--last", type=int, default=20, help="how many recent decisions to show (default 20)")
    ap.add_argument("--since", help="only records with ts >= this (ISO date)")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)

    files = [Path(f).expanduser() for f in ns.files] or default_files(ns.all)
    records = read_records(files)
    if ns.since:
        records = [r for r in records if str(r.get("ts", "")) >= ns.since]

    by_rule = Counter((r.get("rule") or "-", r.get("decision") or "-") for r in records)
    by_profile = Counter(r.get("profile") or "-" for r in records)
    by_outcome = Counter(r.get("outcome") or "-" for r in records)
    by_profile_rule = Counter((r.get("profile") or "-", r.get("rule") or "-") for r in records)
    last = records[-ns.last:] if ns.last > 0 else []

    if ns.json:
        print(json.dumps({
            "files": [str(f) for f in files], "total": len(records),
            "by_outcome": dict(by_outcome),
            "by_rule": [{"rule": k[0], "decision": k[1], "count": v} for k, v in by_rule.most_common()],
            "by_profile": dict(by_profile.most_common()),
            "by_profile_rule": [{"profile": k[0], "rule": k[1], "count": v} for k, v in by_profile_rule.most_common()],
            "last": last,
        }, indent=2, ensure_ascii=False))
        return 0

    print(f"{len(records)} decisions from {sum(1 for f in files if f.exists())} log file(s)")
    if not records:
        return 0
    print("\nOutcome")
    for k, v in by_outcome.most_common():
        print(f"  {v:>6}  {k}")
    print("\nPer rule")
    for (rule, dec), v in by_rule.most_common():
        print(f"  {v:>6}  {dec:<5}  {rule}")
    print("\nPer profile")
    for k, v in by_profile.most_common():
        print(f"  {v:>6}  {k}")
    print("\nPer profile and rule")
    for (prof, rule), v in by_profile_rule.most_common(30):
        print(f"  {v:>6}  {prof:<28} {rule}")
    print(f"\nLast {len(last)}")
    for r in last:
        print(f"  {str(r.get('ts', ''))[:19]}  {r.get('profile', '-'):<20} {r.get('outcome', '-'):<11} "
              f"{r.get('rule', '-'):<28} {r.get('tool', '-')}: {pe.redact(short_args(r), 120)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
