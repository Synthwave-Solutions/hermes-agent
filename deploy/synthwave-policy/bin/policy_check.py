#!/usr/bin/env python3
"""Explain the autonomy decision for one tool call.

    policy_check.py <tool> '<json args>' [--profile NAME] [--policy FILE] [--json]

Examples:
    policy_check.py terminal '{"command": "waapi send --to 316 --text hi"}'
    policy_check.py mcp__claude_ai_Gmail__send_message '{"to": "x@y.nl"}' --profile marcel
    policy_check.py --list --profile synthwave-cmo        # effective rules for a bot

Exit code: 0 AUTO, 10 ASK, 20 BLOCK, 2 usage or policy error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "synthwave-policy", HERE):
    if (candidate / "policy_engine.py").exists():
        sys.path.insert(0, str(candidate))
        break

import policy_engine as pe  # noqa: E402

EXIT = {"AUTO": 0, "ASK": 10, "BLOCK": 20}


def current_home() -> Path:
    home_env = os.environ.get("HERMES_HOME", "").strip()
    return Path(home_env).expanduser() if home_env else pe.root_home()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Explain the Synthwave autonomy decision for a tool call.")
    ap.add_argument("tool", nargs="?", help="tool name, e.g. terminal or mcp__server__tool")
    ap.add_argument("args", nargs="?", default="{}", help="tool arguments as JSON")
    ap.add_argument("--profile", default="default", help="profile (bot) name, default: default")
    ap.add_argument("--policy", help="one policy file (default: the root ~/.hermes/policy/autonomy.yaml plus the "
                                     "profile's own tighten-only file in $HERMES_HOME/policy/)")
    ap.add_argument("--json", action="store_true", help="machine readable output")
    ap.add_argument("--list", action="store_true", help="list the effective rules for the profile")
    ns = ap.parse_args(argv)

    if ns.policy:
        path = Path(ns.policy).expanduser()
        try:
            policy = pe.load_policy_file(path)
        except pe.PolicyError as exc:
            print(f"Policy error in {path}: {exc}", file=sys.stderr)
            return 2
        eff = pe.Effective(policies=[(path, policy)], errors=[], mode=policy.mode_for(ns.profile))
    else:
        home = current_home()
        eff = pe.load_effective(home, ns.profile, pe.PolicyCache())
        if eff.fail_closed:
            print(f"BLOCK: every call is refused: {eff.fail_closed}", file=sys.stderr)
            return 20
        for epath, exc in eff.errors:
            print(f"Policy error in {epath}: {exc} (ignored in observe mode)", file=sys.stderr)
        if eff.inert:
            print("No policy file found (policy/autonomy.yaml). Everything is AUTO.", file=sys.stderr)
            return 2
        path, policy = eff.policies[0]

    if ns.list:
        rows = [{"id": r.id, "decision": d, "base_decision": r.decision, "origin": r.origin,
                 "tags": r.tags, "description": r.description} for r, d in policy.rules_for(ns.profile)]
        if ns.json:
            print(json.dumps({"profile": ns.profile, "mode": policy.mode_for(ns.profile),
                              "default": policy.default_for(ns.profile), "rules": rows}, indent=2,
                             ensure_ascii=False))
        else:
            print(f"profile {ns.profile} | mode {policy.mode_for(ns.profile)} | "
                  f"default {policy.default_for(ns.profile)} | {path}")
            for row in rows:
                changed = "" if row["decision"] == row["base_decision"] else f" (global: {row['base_decision']})"
                print(f"  {row['decision']:<5} {row['id']:<32} {row['description']}{changed}")
        return 0

    if not ns.tool:
        ap.error("tool is required (or use --list)")
    try:
        args = json.loads(ns.args) if ns.args else {}
    except json.JSONDecodeError as exc:
        print(f"args is not valid JSON: {exc}", file=sys.stderr)
        return 2
    decision = eff.evaluate(ns.tool, args, ns.profile)
    if ns.json:
        out = decision.to_dict()
        out.update({"tool": ns.tool, "policy": ", ".join(str(p) for p, _ in eff.policies)})
        if ns.tool.lower() in pe.TERMINAL_TOOLS:
            out["parsed_commands"] = [s.normalized for s in pe.parse_command(args.get("command", ""))]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        effect = {"AUTO": "allowed", "ASK": "needs human approval", "BLOCK": "refused"}[decision.decision]
        if decision.mode == "observe" and decision.decision != "AUTO":
            effect += " (observe mode: only logged for now)"
        print(f"{decision.decision}: {effect}")
        print(f"profile: {decision.profile} | mode: {decision.mode}")
        if decision.rule:
            print(f"rule: {decision.rule.id} ({decision.rule.origin})")
            print(f"why: {decision.rule.description}")
        else:
            print("rule: none matched, profile default applies")
        others = [f"{r.id}={d}" for r, d in decision.matched if decision.rule is None or r.id != decision.rule.id]
        if others:
            print("also matched: " + ", ".join(others))
    return EXIT[decision.decision]


if __name__ == "__main__":
    sys.exit(main())
