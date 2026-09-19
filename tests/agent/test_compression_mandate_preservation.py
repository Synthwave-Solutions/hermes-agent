"""The assignment must survive every compaction round, word for word.

On 18 September 2026 an agent was told to run 171 QA cases with per-case
evidence. Five hours later it had produced reports without runs and screenshots
that showed the wrong page. The compaction of that session recorded the task as
'still going? remember i need the updated url at the end': a passing question
from the user, promoted over the assignment by ``_latest_user_task_snapshot``,
which walks ``reversed(messages)``.

The mandate itself did survive that day, but as summarizer prose under a heading
('## Active Task') that this template never emits and nothing grounds. It
survived by luck, and luck does not survive four rounds.

Governance Decay (arXiv:2606.22528) measures what that costs across seven model
families: a standing instruction that survives compaction gives 0% constraint
violation, one that is dropped gives 38%, and soft deployment-specific rules
decay 8.3x harder than the hard safety norms a model refuses intrinsically.
These tests pin the mechanism that keeps ours on the surviving side.
"""

from __future__ import annotations

from agent.context_compressor import (
    PINNED_MANDATE_HEADING,
    SUMMARY_PREFIX,
    ContextCompressor,
    _PINNED_MANDATE_MAX_CHARS,
)

MANDAAT = (
    "Run the full end to end test suite for adams including every user story, "
    "screenshots against every single test as evidence, an html report, and "
    "host it on gcp with a url when you are done"
)
TERLOOPS = "still going? remember i need the updated url at the end"


def _turns(*teksten: str) -> list[dict]:
    """A realistic compaction window: the mandate first, chatter after."""
    out: list[dict] = []
    for t in teksten:
        out.append({"role": "user", "content": t})
        out.append({"role": "assistant", "content": "ok"})
    return out


def test_snapshot_anchors_forward_not_backward():
    """The mandate comes from the FIRST real user turn, not the last."""
    msgs = _turns(MANDAAT, "why did so much fail?", TERLOOPS)

    pinned = ContextCompressor._pinned_mandate_snapshot(msgs)
    historical = ContextCompressor._latest_user_task_snapshot(msgs)

    assert pinned is not None
    assert "adams" in pinned and "screenshots" in pinned
    # The historical anchor is allowed to pick the newest turn; that is what it
    # is for. The point is that the two no longer share one slot.
    assert historical is not None and TERLOOPS in historical
    assert TERLOOPS not in pinned


def test_scaffolding_turns_cannot_become_the_mandate():
    """Background-process reports occupy the user role; they are not the ask."""
    msgs = [
        {"role": "user", "content": "[IMPORTANT: Background process proc_1 completed (exit_code=0)]"},
        {"role": "assistant", "content": "noted"},
    ] + _turns(MANDAAT)

    pinned = ContextCompressor._pinned_mandate_snapshot(msgs)
    assert pinned is not None
    assert "Background process" not in pinned
    assert "adams" in pinned


def test_mandate_is_written_as_its_own_section():
    c = ContextCompressor.__new__(ContextCompressor)
    c._pinned_mandate = MANDAAT

    grounded = c._ground_pinned_mandate("## Goal\nSomething the model wrote.")

    assert grounded.startswith(PINNED_MANDATE_HEADING)
    assert MANDAAT in grounded
    assert "## Goal" in grounded, "existing sections must survive the injection"


def test_mandate_survives_four_rounds_of_a_paraphrasing_summarizer():
    """The failure mode is gradual: each round keeps the gist and loses a clause.

    Here the summarizer is hostile in the cheapest realistic way: it returns a
    summary that no longer contains the mandate at all. The grounding has to put
    it back every single round, byte for byte, or round four is a different job
    from round one.
    """
    c = ContextCompressor.__new__(ContextCompressor)
    c._pinned_mandate = MANDAAT
    c._mandate_reinjections = 0

    summary = "## Goal\nDo the thing."
    for ronde in range(4):
        # what a paraphrasing summarizer hands back: everything but the mandate
        summary = f"## Goal\nIteration {ronde}: continue the work discussed above."
        summary = c._ground_pinned_mandate(summary)
        assert c._mandate_survived(summary), f"mandate lost in round {ronde}"
        assert MANDAAT in summary

    # and it must appear exactly once, not stack up four deep
    assert summary.count(PINNED_MANDATE_HEADING) == 1


def test_survival_check_catches_a_dropped_mandate():
    c = ContextCompressor.__new__(ContextCompressor)
    c._pinned_mandate = MANDAAT

    assert not c._mandate_survived("## Goal\nno mandate here")
    assert not c._mandate_survived(f"{PINNED_MANDATE_HEADING}\nsomething else entirely")
    assert c._mandate_survived(c._ground_pinned_mandate("## Goal\nx"))


def test_no_mandate_is_not_a_failure():
    """Cron and agent-only sessions have no user turn; that is legal."""
    c = ContextCompressor.__new__(ContextCompressor)
    c._pinned_mandate = None

    assert c._mandate_survived("anything at all")
    assert c._ground_pinned_mandate("## Goal\nx") == "## Goal\nx"
    assert ContextCompressor._pinned_mandate_snapshot(
        [{"role": "assistant", "content": "cron work"}]
    ) is None


def test_long_mandate_is_bounded_but_not_gutted():
    lang = "Run the suite. " + ("Also handle edge case number %d. " % 1) * 400
    msgs = _turns(lang)

    pinned = ContextCompressor._pinned_mandate_snapshot(msgs)
    assert pinned is not None
    assert len(pinned) <= _PINNED_MANDATE_MAX_CHARS
    assert pinned.startswith("Run the suite.")


def test_prefix_names_the_pinned_section_and_keeps_the_80622_guard():
    """The carve-out must not reopen the bug it sits next to.

    #80622: a standalone handoff with nothing after it must not resume work or
    call tools. Naming the mandate tells the model WHAT the job is; it must not
    tell it to start on silence.
    """
    lower = SUMMARY_PREFIX.lower()

    assert PINNED_MANDATE_HEADING in SUMMARY_PREFIX
    assert "does not authorise you to act on your own" in lower
    # the original guard, unchanged
    assert "if no user message appears after this summary" in lower
    assert "do nothing" in lower
    assert "wait for a new user message" in lower
    assert "must never become the active turn" in lower
    # and a later instruction still wins over the pin
    assert "a later message replacing it wins" in lower
    assert "silence does not" in lower
