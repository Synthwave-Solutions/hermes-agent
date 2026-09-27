"""``hermes cron`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` — same arguments, same
``func=cmd_cron`` dispatch. The handler is injected so this module does not
import ``main`` (cycle avoidance).
"""

from __future__ import annotations

from typing import Callable

from hermes_cli.subcommands._shared import add_accept_hooks_flag


def _cli_actor() -> str:
    """Who is running this command, for the owner audit log."""
    try:
        import getpass

        user = getpass.getuser()
    except Exception:
        user = ""
    return f"os:{user or 'unknown'}"


def cmd_cron_reassign_owner(args) -> int:
    """``hermes cron reassign-owner``: the audited admin path for owner changes.

    Exit codes: 0 done (or already owned by that address), 1 refused or
    failed, 2 usage error.
    """
    from cron.jobs import (
        AmbiguousJobReference,
        cron_system_principal,
        owner_audit_file,
        reassign_job_owner,
    )
    from hermes_cli.colors import Colors, color

    owner = str(getattr(args, "owner", None) or "").strip()
    use_principal = bool(getattr(args, "system_principal", False))
    if bool(owner) == use_principal:
        print(color("Give exactly one new owner: an email address or --system-principal.", Colors.RED))
        return 2
    if use_principal:
        owner = cron_system_principal()
        if not owner:
            print(color(
                "cron.system_principal is not set in config.yaml (or is not a valid address).",
                Colors.RED,
            ))
            return 1
    try:
        result = reassign_job_owner(
            args.job_id,
            owner,
            actor=_cli_actor(),
            reason=str(getattr(args, "reason", "") or ""),
            source="cli",
        )
    except AmbiguousJobReference as exc:
        print(color(str(exc), Colors.RED))
        for match in exc.matches:
            print(f"  {match['id']}  (name: {match.get('name')!r})")
        return 1
    except (PermissionError, ValueError) as exc:
        print(color(f"Failed to reassign owner: {exc}", Colors.RED))
        return 1
    if result is None:
        print(color(f"Job not found: {args.job_id}", Colors.RED))
        return 1
    job = result["job"]
    label = f"{job.get('name') or job['id']} ({job['id']})"
    if not result["changed"]:
        print(f"Owner unchanged: {label} already belongs to {result['new_owner']}")
        return 0
    print(color(f"Reassigned job: {label}", Colors.GREEN))
    print(f"  Owner: {result['previous_owner'] or '(none)'} -> {result['new_owner']}")
    print(f"  Audit: {owner_audit_file()}")
    return 0


def build_cron_parser(subparsers, *, cmd_cron: Callable) -> None:
    """Attach the ``cron`` subcommand (and its sub-actions) to ``subparsers``."""
    cron_parser = subparsers.add_parser(
        "cron", help="Cron job management", description="Manage scheduled tasks"
    )
    cron_subparsers = cron_parser.add_subparsers(dest="cron_command")

    # cron list
    cron_list = cron_subparsers.add_parser("list", help="List scheduled jobs")
    cron_list.add_argument("--all", action="store_true", help="Include disabled jobs")

    # cron create/add
    cron_create = cron_subparsers.add_parser(
        "create", aliases=["add"], help="Create a scheduled job"
    )
    cron_create.add_argument(
        "schedule", help="Schedule like '30m', 'every 2h', or '0 9 * * *'"
    )
    cron_create.add_argument(
        "prompt", nargs="?", help="Optional self-contained prompt or task instruction"
    )
    cron_create.add_argument("--name", help="Optional human-friendly job name")
    cron_create.add_argument(
        "--deliver",
        help=(
            "Delivery target: origin, local, telegram, discord, signal, "
            "platform:chat_id, or bot-chat[:profile] (inject output into a "
            "local profile's canonical Bot Chat as a message the bot responds to)"
        ),
    )
    cron_create.add_argument("--repeat", type=int, help="Optional repeat count")
    cron_create.add_argument(
        "--skill",
        dest="skills",
        action="append",
        help="Attach a skill. Repeat to add multiple skills.",
    )
    cron_create.add_argument(
        "--script",
        help=(
            "Path to a script under ~/.hermes/scripts/. Default mode: "
            "script stdout is injected into the agent's prompt each run. "
            "With --no-agent: the script IS the job and its stdout is "
            "delivered verbatim. .sh/.bash files run via bash, everything "
            "else via Python."
        ),
    )
    cron_create.add_argument(
        "--no-agent",
        dest="no_agent",
        action="store_true",
        default=False,
        help=(
            "Skip the LLM entirely — run --script on schedule and deliver "
            "its stdout directly. Empty stdout = silent. Classic watchdog "
            "pattern (memory alerts, disk alerts, CI pings)."
        ),
    )
    cron_create.add_argument(
        "--monitor-script",
        dest="monitor_script",
        help=(
            "Monitor mode: path to a cheap source script under "
            "~/.hermes/scripts/ that runs each tick BEFORE the agent. "
            "Unchanged output (exact-bytes hash) suppresses the agent run "
            "entirely; changed output injects a MONITOR CHANGE DETECTED "
            "diff into the prompt. Script output must be stable (no "
            "timestamps). Mutually exclusive with --monitor-url; "
            "incompatible with --no-agent."
        ),
    )
    cron_create.add_argument(
        "--monitor-url",
        dest="monitor_url",
        help=(
            "Monitor mode: http(s) URL fetched with a bounded GET each tick "
            "instead of a script. Same hash-suppression semantics as "
            "--monitor-script."
        ),
    )
    cron_create.add_argument(
        "--workdir",
        help="Absolute path for the job to run from. Injects AGENTS.md / CLAUDE.md / .cursorrules from that directory and uses it as the cwd for terminal/file/code_exec tools. Omit to preserve old behaviour (no project context files).",
    )
    cron_create.add_argument(
        "--model",
        help=(
            "Pin this job to a specific inference model (user-owned; the "
            "agent's cronjob tool cannot set this). Omit to follow "
            "cron.model / model.default from config.yaml."
        ),
    )
    cron_create.add_argument(
        "--provider",
        dest="model_provider",
        help="Inference provider paired with --model (e.g. 'openrouter', 'nous').",
    )
    cron_create.add_argument(
        "--reasoning-effort",
        dest="reasoning_effort",
        help=(
            "Pin this job's reasoning (thinking) effort: none, minimal, low, "
            "medium, high, xhigh, max, or ultra. Overrides agent.reasoning_effort "
            "and agent.reasoning_overrides for this job; unsupported levels are "
            "clamped by the provider at request time. Omit to follow config."
        ),
    )
    cron_create.add_argument(
        "--continuity",
        dest="continuity",
        action="store_const",
        const=True,
        default=None,
        help=(
            "Each run wakes up with the job's own previous output injected "
            "into its prompt, so it can dedupe against what was already "
            "reported and continue where the last run left off (scouts, "
            "monitors, incremental digests). First run is unchanged."
        ),
    )

    # cron edit
    cron_edit = cron_subparsers.add_parser(
        "edit", help="Edit an existing scheduled job"
    )
    cron_edit.add_argument("job_id", help="Job ID to edit")
    cron_edit.add_argument("--schedule", help="New schedule")
    cron_edit.add_argument("--prompt", help="New prompt/task instruction")
    cron_edit.add_argument("--name", help="New job name")
    cron_edit.add_argument("--deliver", help="New delivery target")
    cron_edit.add_argument("--repeat", type=int, help="New repeat count")
    cron_edit.add_argument(
        "--skill",
        dest="skills",
        action="append",
        help="Replace the job's skills with this set. Repeat to attach multiple skills.",
    )
    cron_edit.add_argument(
        "--add-skill",
        dest="add_skills",
        action="append",
        help="Append a skill without replacing the existing list. Repeatable.",
    )
    cron_edit.add_argument(
        "--remove-skill",
        dest="remove_skills",
        action="append",
        help="Remove a specific attached skill. Repeatable.",
    )
    cron_edit.add_argument(
        "--clear-skills",
        action="store_true",
        help="Remove all attached skills from the job",
    )
    cron_edit.add_argument(
        "--script",
        help=(
            "Path to a script under ~/.hermes/scripts/. Pass empty string to clear. "
            "With --no-agent the script IS the job; otherwise its stdout is "
            "injected into the agent's prompt each run."
        ),
    )
    cron_edit.add_argument(
        "--no-agent",
        dest="no_agent",
        action="store_const",
        const=True,
        default=None,
        help=(
            "Enable no-agent mode on this job (requires --script or an "
            "existing script on the job)."
        ),
    )
    cron_edit.add_argument(
        "--agent",
        dest="no_agent",
        action="store_const",
        const=False,
        help="Disable no-agent mode on this job (reverts to LLM-driven execution).",
    )
    cron_edit.add_argument(
        "--continuity",
        dest="continuity",
        action="store_const",
        const=True,
        default=None,
        help=(
            "Turn on run-to-run continuity: each run sees the job's own "
            "previous output (dedupe, continue where it left off)."
        ),
    )
    cron_edit.add_argument(
        "--no-continuity",
        dest="continuity",
        action="store_const",
        const=False,
        help=(
            "Turn off run-to-run continuity (other context_from job refs "
            "are preserved)."
        ),
    )
    cron_edit.add_argument(
        "--monitor-script",
        dest="monitor_script",
        help=(
            "Set/replace the monitor source script (see `hermes cron create "
            "--monitor-script`). Pass empty string to clear."
        ),
    )
    cron_edit.add_argument(
        "--monitor-url",
        dest="monitor_url",
        help=(
            "Set/replace the monitor source URL. Pass empty string to clear."
        ),
    )
    cron_edit.add_argument(
        "--workdir",
        help="Absolute path for the job to run from (injects AGENTS.md etc. and sets terminal cwd). Pass empty string to clear.",
    )
    cron_edit.add_argument(
        "--model",
        help=(
            "Pin this job to a specific inference model (user-owned; the "
            "agent's cronjob tool cannot set this). Pass empty string to "
            "clear the pin and follow cron.model / model.default."
        ),
    )
    cron_edit.add_argument(
        "--provider",
        dest="model_provider",
        help="Inference provider paired with --model. Pass empty string to clear.",
    )
    cron_edit.add_argument(
        "--reasoning-effort",
        dest="reasoning_effort",
        help=(
            "Pin this job's reasoning (thinking) effort: none, minimal, low, "
            "medium, high, xhigh, max, or ultra. Pass empty string to clear "
            "the pin and follow config resolution."
        ),
    )

    # lifecycle actions
    cron_pause = cron_subparsers.add_parser("pause", help="Pause a scheduled job")
    cron_pause.add_argument("job_id", help="Job ID to pause")

    cron_resume = cron_subparsers.add_parser("resume", help="Resume a paused job")
    cron_resume.add_argument("job_id", help="Job ID to resume")
    cron_resume.add_argument("--at", dest="run_at", help="Re-arm at an ISO-8601 time")
    cron_resume.add_argument("--run-now", action="store_true", help="Re-arm to run now")

    cron_run = cron_subparsers.add_parser(
        "run", help="Run a job on the next scheduler tick"
    )
    cron_run.add_argument("job_id", help="Job ID to trigger")
    add_accept_hooks_flag(cron_run)

    cron_remove = cron_subparsers.add_parser(
        "remove", aliases=["rm", "delete"], help="Remove a scheduled job"
    )
    cron_remove.add_argument("job_id", help="Job ID to remove")

    # cron status
    cron_subparsers.add_parser("status", help="Check if cron scheduler is running")

    cron_runs = cron_subparsers.add_parser(
        "runs", aliases=["history"], help="Show durable execution attempts"
    )
    cron_runs.add_argument("job_id", nargs="?", help="Optional job ID filter")
    cron_runs.add_argument("--limit", type=int, default=20, help="Rows to show (1-500)")

    # cron incidents — durable failure incidents (list/ack)
    cron_incidents = cron_subparsers.add_parser(
        "incidents", help="List or acknowledge durable cron failure incidents"
    )
    cron_incidents.add_argument(
        "--state",
        choices=["detected", "alerted", "closed"],
        help="Filter incidents by lifecycle state",
    )
    cron_incidents.add_argument(
        "incident_action",
        nargs="?",
        default="list",
        choices=["list", "ack"],
        help="Action (default: list)",
    )
    cron_incidents.add_argument(
        "incident_id", nargs="?", help="Incident ID to acknowledge (ack)"
    )

    # cron notepad — per-job durable KV scratchpad (injected into the job
    # prompt each run; the running agent writes it via this CLI).
    cron_notepad = cron_subparsers.add_parser(
        "notepad",
        help="Read/write a job's durable notepad (persistent KV across runs)",
    )
    cron_notepad.add_argument("job_id", help="Job ID the notepad belongs to")
    cron_notepad.add_argument(
        "notepad_action",
        nargs="?",
        default="list",
        choices=["get", "set", "delete", "list"],
        help="Action (default: list)",
    )
    cron_notepad.add_argument("key", nargs="?", help="Notepad key (get/set/delete)")
    cron_notepad.add_argument("value", nargs="?", help="Value to store (set)")

    # cron reassign-owner: the only way to change who a job runs as. Editing a
    # job cannot touch its owner (cron.jobs refuses the identity fields), so
    # this admin path is dispatched directly, not through ``cmd_cron``.
    cron_reassign = cron_subparsers.add_parser(
        "reassign-owner",
        help="Give a scheduled job a new owner (administrators only; audited)",
        description=(
            "Change the person a scheduled job runs as. The owner of a job "
            "cannot be changed by editing it; this command is the only way. "
            "It is refused inside a governed session, and every change is "
            "appended to cron/owner-audit.jsonl."
        ),
    )
    cron_reassign.add_argument("job_id", help="Job ID or name")
    cron_reassign.add_argument(
        "owner", nargs="?", help="Email address of the new owner"
    )
    cron_reassign.add_argument(
        "--system-principal",
        dest="system_principal",
        action="store_true",
        default=False,
        help="Give the job to cron.system_principal from config.yaml",
    )
    cron_reassign.add_argument(
        "--reason", default="", help="Why the owner changes (kept in the audit log)"
    )
    cron_reassign.set_defaults(func=cmd_cron_reassign_owner)

    # cron doctor
    cron_subparsers.add_parser("doctor", help="Check scheduled jobs for common health issues")

    # cron tick (mostly for debugging)
    cron_tick = cron_subparsers.add_parser("tick", help="Run due jobs once and exit")
    add_accept_hooks_flag(cron_tick)
    add_accept_hooks_flag(cron_parser)
    cron_parser.set_defaults(func=cmd_cron)
