"""Confirmed native skill writes remain observable when a review ends early.

The actual worker, skill tools, summary function and cleanup execute. Only the
provider-capable fork factory and outbound skill-sync transport are replaced.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from agent import background_review as review
import hermes_constants
from tools import skill_manager_tool, skill_provenance, skills_tool, terminal_tool


SKILL_NAME = "partial-completion-fixture"
SKILL_CONTENT = (
    "---\nname: partial-completion-fixture\n"
    "description: Synthetic local completion regression\n---\n"
    "# Workflow\nOriginal step.\n"
)


@pytest.fixture
def worker(monkeypatch, tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    home_token = hermes_constants.set_hermes_home_override(home)
    monkeypatch.setattr(skill_manager_tool, "_maybe_debounced_sync_push", lambda *_: None)
    original_summary = review.summarize_background_review_actions

    def run(*, changes="create_patch", runtime_error=None, startup_error=None,
            cleanup_error=None, notification_mode="on", observer_error=None,
            cancel_before_start=False, expected_escape=None):
        events, observed, summary_results, notifications, failures = [], [], [], [], []
        prior = [{"role": "user", "content": "Synthetic completed task"}]

        class Fork:
            _memory_enabled = False
            _user_profile_enabled = False
            _session_messages = None

            def __init__(self):
                self._session_messages = []
                self.emitted = []

            def call_skill(self, arguments):
                result = skill_manager_tool.skill_manage(**arguments)
                call_id = f"native-skill-{len(self.emitted)}"
                pair = [
                    {"role": "assistant", "tool_calls": [{
                        "id": call_id, "function": {
                            "name": "skill_manage", "arguments": json.dumps(arguments),
                        },
                    }]},
                    {"role": "tool", "tool_call_id": call_id, "content": result},
                ]
                self._session_messages.extend(pair)
                self.emitted.extend(deepcopy(pair))
                return json.loads(result)

            def run_conversation(self, *, user_message, conversation_history):
                events.append("run")
                self._session_messages = deepcopy(conversation_history)
                origin = skill_provenance.set_current_write_origin("background_review")
                try:
                    if changes == "create_patch":
                        created = self.call_skill({
                            "action": "create", "name": SKILL_NAME,
                            "content": SKILL_CONTENT,
                        })
                        assert created["success"] is True, created
                        skills_tool.skill_view(SKILL_NAME)
                        patched = self.call_skill({
                            "action": "patch", "name": SKILL_NAME,
                            "old_string": "Original step.", "new_string": "Confirmed step.",
                        })
                        assert patched["success"] is True, patched
                    elif changes == "failed_patch":
                        denied = self.call_skill({
                            "action": "patch", "name": "absent-fixture",
                            "old_string": "missing", "new_string": "never written",
                        })
                        assert denied["success"] is False, denied
                    if runtime_error is not None:
                        raise runtime_error
                    return {"final_response": "Synthetic completion"}
                finally:
                    skill_provenance.reset_current_write_origin(origin)

            def shutdown_memory_provider(self):
                events.append("shutdown")

            def close(self):
                events.append("close")
                # Model real teardown that clears the fork's transient history.
                self._session_messages.clear()
                if cleanup_error is not None:
                    raise cleanup_error

        fork = Fork()

        def build(*args, **kwargs):
            events.append("build")
            if startup_error is not None:
                raise startup_error
            return fork, {}, False

        def observe(messages, prior_snapshot, notification_mode="on"):
            events.append("summarize")
            observed.append((deepcopy(messages), deepcopy(prior_snapshot), notification_mode))
            result = original_summary(messages, prior_snapshot, notification_mode)
            summary_results.append(result)
            if observer_error is not None:
                raise observer_error
            return result

        parent = SimpleNamespace(
            client=None, session_id="synthetic-parent", _session_db=None,
            _active_children=[], _background_review_agent=None,
            memory_notifications=notification_mode,
            background_review_callback=notifications.append,
            _safe_print=lambda *_: None,
            _emit_auxiliary_failure=lambda name, error: failures.append((name, error)),
        )
        monkeypatch.setattr(review, "build_cache_parity_fork", build)
        monkeypatch.setattr(review, "summarize_background_review_actions", observe)
        run_token = review.prepare_background_review_run(parent)
        assert run_token is not None
        if cancel_before_start:
            run_token.cancel()

        def invoke():
            return review._run_review_in_thread(
                parent, prior, "Synthetic local review", task_cfg={}, review_run=run_token,
            )

        if expected_escape is not None:
            with pytest.raises(type(expected_escape)) as escaped:
                invoke()
            assert escaped.value is expected_escape
            result = None
        else:
            result = invoke()
        assert result is None
        assert parent._active_children == []
        assert parent._background_review_agent is None
        assert parent._background_review_run is None
        assert run_token.request_done.is_set()
        assert terminal_tool._get_approval_callback() is None
        return SimpleNamespace(
            events=events, observed=observed, summaries=summary_results,
            notifications=notifications, failures=failures, prior=prior,
            emitted=fork.emitted, remaining_messages=fork._session_messages,
            skill_file=home / "skills" / SKILL_NAME / "SKILL.md",
        )

    try:
        yield run
    finally:
        hermes_constants.reset_hermes_home_override(home_token)


def assert_observed_once_before_teardown(result):
    assert len(result.observed) == 1
    messages, prior, _mode = result.observed[0]
    assert prior == result.prior
    assert messages == result.prior + result.emitted
    assert result.events.index("summarize") < result.events.index("shutdown")
    assert result.events.index("summarize") < result.events.index("close")
    assert result.remaining_messages == []


@pytest.mark.parametrize("raise_after_writes", [False, True], ids=["success", "later-failure"])
def test_confirmed_create_and_patch_are_observed_once_before_teardown(worker, raise_after_writes):
    error = RuntimeError("synthetic provider failure") if raise_after_writes else None
    result = worker(runtime_error=error)
    assert result.skill_file.read_text(encoding="utf-8").endswith("Confirmed step.\n")
    native_results = [json.loads(row["content"]) for row in result.emitted
                      if row["role"] == "tool"]
    assert [outcome["success"] for outcome in native_results] == [True, True]
    assert_observed_once_before_teardown(result)
    # Preserve the real native result messages, including their casing, instead
    # of inventing separate display text for an observer-only lifecycle fix.
    assert result.summaries == [[outcome["message"] for outcome in native_results]]
    assert len(result.notifications) == (0 if error else 1)
    assert result.failures == ([("background review", error)] if error else [])


@pytest.mark.parametrize("raise_after_writes", [False, True], ids=["success", "later-failure"])
def test_notification_off_still_observes_confirmed_results_without_visible_summary(worker, raise_after_writes):
    error = RuntimeError("synthetic provider failure") if raise_after_writes else None
    result = worker(runtime_error=error, notification_mode="off")
    assert result.skill_file.read_text(encoding="utf-8").endswith("Confirmed step.\n")
    assert_observed_once_before_teardown(result)
    assert result.observed[0][2] == "off"
    assert result.summaries == [[]]
    assert result.notifications == []
    assert result.failures == ([("background review", error)] if error else [])


def test_failed_native_patch_is_observable_without_fabricated_change(worker):
    error = RuntimeError("synthetic failure after denied patch")
    result = worker(changes="failed_patch", runtime_error=error)
    assert not result.skill_file.exists()
    assert_observed_once_before_teardown(result)
    assert result.summaries == [[]]
    assert result.notifications == []
    assert result.failures == [("background review", error)]


def test_failure_before_any_tool_result_reports_failure_and_cleans_up(worker):
    error = RuntimeError("synthetic failure before tool results")
    result = worker(changes="none", runtime_error=error)
    assert result.emitted == []
    assert not result.skill_file.exists()
    assert len(result.observed) <= 1
    assert all(not summary for summary in result.summaries)
    assert result.notifications == []
    assert result.failures == [("background review", error)]
    assert result.events.count("close") == 1


def test_startup_failure_does_not_fabricate_review_results(worker):
    error = RuntimeError("synthetic fork startup failure")
    result = worker(startup_error=error)
    assert result.events == ["build"]
    assert result.observed == []
    assert result.notifications == []
    assert result.failures == [("background review", error)]


def test_teardown_failure_keeps_confirmed_results_and_original_runtime_failure(worker):
    error = RuntimeError("synthetic original provider failure")
    result = worker(runtime_error=error, cleanup_error=RuntimeError("synthetic close failure"))
    assert_observed_once_before_teardown(result)
    assert result.failures == [("background review", error)]
    assert result.events.count("close") == 1


def test_summary_observer_failure_does_not_duplicate_or_prevent_cleanup(worker):
    result = worker(observer_error=RuntimeError("synthetic observer failure"))
    assert_observed_once_before_teardown(result)
    assert result.notifications == []
    assert result.failures == []
    assert result.events.count("close") == 1


def test_cancellation_after_confirmed_writes_observes_once_and_propagates(worker):
    cancellation = asyncio.CancelledError("synthetic cancellation after writes")
    result = worker(runtime_error=cancellation, expected_escape=cancellation)
    assert result.skill_file.read_text(encoding="utf-8").endswith("Confirmed step.\n")
    assert_observed_once_before_teardown(result)
    assert len(result.summaries[0]) == 2
    assert result.notifications == []
    assert result.failures == []
    assert result.events.count("close") == 1


def test_cancelled_start_never_admits_fork_or_fabricates_skill_change(worker):
    result = worker(cancel_before_start=True)
    assert result.events == []
    assert result.emitted == []
    assert not result.skill_file.exists()
    assert result.observed == []
    assert result.notifications == []
    assert result.failures == []
