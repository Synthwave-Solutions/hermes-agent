"""Server-owned metadata for one cron fire, never a user-defined environment."""
from __future__ import annotations

MAX_RUN_PROMPT_BYTES = 8192
RUN_CONTEXT_FIELDS = frozenset({"manual_run_at", "manual_run_prompt", "manual_run_trigger", "manual_run_actor", "_cron_run_context"})


def normalize_context(trigger="schedule", trigger_actor=None, run_prompt=None):
    if trigger not in {"schedule", "manual", "api"}:
        raise ValueError("cron trigger must be schedule, manual or api")
    actor = trigger_actor or ""
    prompt = run_prompt or ""
    if not isinstance(actor, str) or len(actor.encode("utf-8")) > 512 or any(ord(c) < 32 for c in actor):
        raise ValueError("invalid cron trigger actor")
    if not isinstance(prompt, str) or "\0" in prompt or len(prompt.encode("utf-8")) > MAX_RUN_PROMPT_BYTES:
        raise ValueError("cron run prompt must be at most 8192 UTF-8 bytes without NUL")
    # A recurring fire must never inherit a previous approval or per-run input.
    return {"trigger": trigger, "trigger_actor": actor if trigger != "schedule" else "",
            "run_prompt": prompt if trigger != "schedule" else ""}


def context_for_job(job, *, trigger=None, trigger_actor=None, run_prompt=None):
    if trigger is not None:
        return normalize_context(trigger, trigger_actor, run_prompt)
    if job.get("_cron_run_context") is not None:
        return normalize_context(**job["_cron_run_context"])
    if job.get("manual_run_at"):
        return normalize_context(job.get("manual_run_trigger") or "manual", job.get("manual_run_actor"),
                                 run_prompt if run_prompt is not None else job.get("manual_run_prompt"))
    return normalize_context()


def governed_trigger_actor():
    """Only the bound caller or inherited governed-shell identity, never model arguments."""
    from hermes_cli.dashboard_governance.context import current_governance_context
    ctx = current_governance_context()
    if ctx is not None:
        return str(getattr(getattr(getattr(ctx, "access", None), "subject", None), "email", "") or "")
    import os
    return os.environ.get("HERMES_DWD_IDENTITY", "")


def script_environment(job):
    from cron.notify_policy import resolve_notify
    ctx = context_for_job(job)
    notify = resolve_notify(job)
    return {
        "HERMES_CRON_JOB_ID": str(job.get("id") or ""),
        "HERMES_CRON_JOB_NAME": str(job.get("name") or job.get("id") or ""),
        "HERMES_CRON_TRIGGER": ctx["trigger"],
        "HERMES_CRON_TRIGGER_ACTOR": ctx["trigger_actor"],
        "HERMES_CRON_RUN_PROMPT": ctx["run_prompt"],
        "HERMES_CRON_NOTIFY_DELIVERY": notify["delivery"],
        "HERMES_CRON_NOTIFY_IN_APP": notify["in_app"],
        "HERMES_CRON_MANAGED_BY": str(job.get("managed_by") or ""),
    }


def validate_script_overlay(overlay):
    """Only the fixed contract can overlay the scrubbed child environment."""
    allowed = set(script_environment({}))
    if not isinstance(overlay, dict) or set(overlay) - allowed:
        raise ValueError("cron script environment accepts only server-owned metadata")
    if any(not isinstance(v, str) or "\0" in v for v in overlay.values()):
        raise ValueError("invalid cron script metadata")
    return overlay
