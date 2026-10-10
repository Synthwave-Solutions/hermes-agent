"""Contract tests for cron/notify_policy.py (the per-job notification policy).

The WebUI ships a byte copy of this module, so these tests pin behaviour that
both sides depend on: level values, legacy mapping, defaults for new jobs, the
delivery gate, the in-app flag table and value normalisation.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import sys
from pathlib import Path

import pytest

from cron import notify_policy as policy_module
from cron.notify_policy import (
    ALERT_KINDS,
    LEVELS_DELIVERY,
    LEVELS_IN_APP,
    MANAGED_BY,
    OUTCOME_MUTED,
    UI_IN_APP_CHOICES,
    combine_flags,
    default_notify_for_new_job,
    in_app_flags,
    legacy_toast_value,
    normalize_managed_by,
    normalize_notify,
    resolve_notify,
    should_deliver,
)

MODULE_PATH = Path(policy_module.__file__)


# ---------------------------------------------------------------------------
# Wire values shared with the WebUI, E1 and the tool schema
# ---------------------------------------------------------------------------


def test_wire_values_match_the_contract():
    assert LEVELS_IN_APP == ("all", "failures", "quiet", "off")
    assert LEVELS_DELIVERY == ("all", "failures", "off")
    assert UI_IN_APP_CHOICES == ("all", "failures", "quiet")
    assert ALERT_KINDS == ("failure", "blocked_config", "drift", "preflight")
    assert OUTCOME_MUTED == "suppressed_muted"
    assert MANAGED_BY == ("synthwave", "client")


def test_ui_choices_never_offer_off():
    assert "off" not in UI_IN_APP_CHOICES
    assert set(UI_IN_APP_CHOICES) < set(LEVELS_IN_APP)


# ---------------------------------------------------------------------------
# normalize_notify
# ---------------------------------------------------------------------------


def test_normalize_none_stays_none():
    assert normalize_notify(None) is None


@pytest.mark.parametrize(
    "in_app,delivery", list(itertools.product(LEVELS_IN_APP, LEVELS_DELIVERY))
)
def test_normalize_accepts_every_level_combination(in_app, delivery):
    assert normalize_notify({"in_app": in_app, "delivery": delivery}) == {
        "in_app": in_app,
        "delivery": delivery,
    }


def test_normalize_fills_missing_keys_from_legacy_defaults():
    assert normalize_notify({}) == {"in_app": "all", "delivery": "all"}
    assert normalize_notify({"in_app": "quiet"}) == {"in_app": "quiet", "delivery": "all"}
    assert normalize_notify({"delivery": "failures"}) == {"in_app": "all", "delivery": "failures"}


def test_normalize_returns_a_new_dict():
    value = {"in_app": "failures", "delivery": "off"}
    result = normalize_notify(value)
    assert result == value
    assert result is not value
    result["in_app"] = "all"
    assert value["in_app"] == "failures"


@pytest.mark.parametrize(
    "value",
    [
        "all",
        "",
        0,
        1,
        True,
        ["all", "all"],
        ("all", "all"),
        {"in_app": "all", "delivery": "all", "extra": "x"},
        {"toast": True},
        {1: "all"},
        {"in_app": "never"},
        {"in_app": "ALL"},
        {"in_app": " all"},
        {"in_app": None},
        {"in_app": True},
        {"in_app": 1},
        {"delivery": "quiet"},
        {"delivery": "never"},
        {"delivery": None},
        {"delivery": ["all"]},
    ],
)
def test_normalize_rejects_anything_else(value):
    with pytest.raises(ValueError):
        normalize_notify(value)


def test_normalize_error_messages_do_not_echo_input():
    with pytest.raises(ValueError) as unknown:
        normalize_notify({"<script>": "all"})
    assert "<script>" not in str(unknown.value)
    with pytest.raises(ValueError) as bad_level:
        normalize_notify({"in_app": "<script>"})
    assert "<script>" not in str(bad_level.value)


# ---------------------------------------------------------------------------
# resolve_notify (explicit and legacy)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job,expected_in_app",
    [
        ({}, "all"),
        ({"toast_notifications": True}, "all"),
        ({"toast_notifications": False}, "quiet"),
        ({"toast_notifications": None}, "all"),
        # Only a real False maps to quiet; other falsy values keep the old default.
        ({"toast_notifications": 0}, "all"),
        ({"toast_notifications": ""}, "all"),
        ({"notify": None, "toast_notifications": False}, "quiet"),
    ],
)
def test_resolve_legacy_mapping(job, expected_in_app):
    assert resolve_notify(job) == {
        "in_app": expected_in_app,
        "delivery": "all",
        "source": "legacy",
    }


@pytest.mark.parametrize(
    "in_app,delivery", list(itertools.product(LEVELS_IN_APP, LEVELS_DELIVERY))
)
def test_resolve_explicit_wins_over_toast_notifications(in_app, delivery):
    for toast in (True, False, None):
        job = {"notify": {"in_app": in_app, "delivery": delivery}, "toast_notifications": toast}
        assert resolve_notify(job) == {
            "in_app": in_app,
            "delivery": delivery,
            "source": "explicit",
        }


def test_resolve_partial_explicit_value_is_filled():
    assert resolve_notify({"notify": {"delivery": "failures"}, "toast_notifications": False}) == {
        "in_app": "all",
        "delivery": "failures",
        "source": "explicit",
    }


@pytest.mark.parametrize(
    "damaged",
    ["failures", {"in_app": "never"}, {"delivery": "sometimes"}, {"bogus": 1}, [1, 2]],
)
def test_resolve_damaged_value_reads_as_legacy(damaged):
    assert resolve_notify({"notify": damaged, "toast_notifications": False}) == {
        "in_app": "quiet",
        "delivery": "all",
        "source": "legacy",
    }


@pytest.mark.parametrize("job", [None, "job", 3])
def test_resolve_non_dict_job_reads_as_legacy(job):
    assert resolve_notify(job) == {"in_app": "all", "delivery": "all", "source": "legacy"}


def test_resolve_does_not_mutate_the_job():
    job = {"notify": {"in_app": "quiet"}, "toast_notifications": True}
    resolve_notify(job)
    assert job == {"notify": {"in_app": "quiet"}, "toast_notifications": True}


# ---------------------------------------------------------------------------
# default_notify_for_new_job
# ---------------------------------------------------------------------------


def test_default_for_monitoring_jobs_is_failures_only_in_app():
    assert default_notify_for_new_job(category="Monitoring") == {
        "in_app": "failures",
        "delivery": "all",
    }


@pytest.mark.parametrize("category", [None, "", "Reports", "monitoring", "Monitoring ", "Other"])
def test_default_for_other_jobs_is_every_run(category):
    assert default_notify_for_new_job(category=category) == {
        "in_app": "all",
        "delivery": "all",
    }


def test_default_requires_keyword_category():
    with pytest.raises(TypeError):
        default_notify_for_new_job("Monitoring")  # type: ignore[misc]


def test_default_is_a_valid_notify_value():
    for category in (None, "Monitoring"):
        value = default_notify_for_new_job(category=category)
        assert normalize_notify(value) == value


# ---------------------------------------------------------------------------
# should_deliver (engine gate at both delivery sites)
# ---------------------------------------------------------------------------

_ALERT_CHOICES = (None,) + ALERT_KINDS + ("something_else",)


def _expected_delivery(delivery, success, alert_kind):
    if delivery == "all":
        return (True, None)
    if delivery == "failures":
        if not success or alert_kind in ALERT_KINDS:
            return (True, None)
        return (False, OUTCOME_MUTED)
    return (False, OUTCOME_MUTED)


@pytest.mark.parametrize(
    "delivery,success,alert_kind",
    list(itertools.product(LEVELS_DELIVERY, (True, False), _ALERT_CHOICES)),
)
def test_should_deliver_every_combination(delivery, success, alert_kind):
    job = {"notify": {"in_app": "all", "delivery": delivery}}
    assert should_deliver(job, success=success, alert_kind=alert_kind) == _expected_delivery(
        delivery, success, alert_kind
    )


def test_failures_only_mutes_success_and_delivers_failure():
    job = {"notify": {"in_app": "all", "delivery": "failures"}}
    assert should_deliver(job, success=True) == (False, "suppressed_muted")
    assert should_deliver(job, success=False) == (True, None)


@pytest.mark.parametrize("alert_kind", ALERT_KINDS)
def test_failures_only_delivers_alerts_even_on_a_successful_run(alert_kind):
    job = {"notify": {"delivery": "failures"}}
    assert should_deliver(job, success=True, alert_kind=alert_kind) == (True, None)


@pytest.mark.parametrize("alert_kind", _ALERT_CHOICES)
@pytest.mark.parametrize("success", [True, False])
def test_off_mutes_everything(success, alert_kind):
    job = {"notify": {"delivery": "off"}}
    assert should_deliver(job, success=success, alert_kind=alert_kind) == (False, OUTCOME_MUTED)


@pytest.mark.parametrize(
    "job", [{}, {"toast_notifications": False}, {"toast_notifications": True}, {"notify": "bad"}]
)
@pytest.mark.parametrize("success", [True, False])
def test_legacy_jobs_always_deliver(job, success):
    assert should_deliver(job, success=success) == (True, None)


def test_should_deliver_keywords_are_required():
    with pytest.raises(TypeError):
        should_deliver({}, True)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# in_app_flags (table in plan section 3.1)
# ---------------------------------------------------------------------------

# level -> (success flags, failure flags) as (toast, badge, desktop)
_TABLE = {
    "all": ((True, True, False), (True, True, True)),
    "failures": ((False, False, False), (True, True, True)),
    "quiet": ((False, True, False), (False, True, False)),
    "off": ((False, False, False), (False, False, False)),
}


def _flags(triple):
    toast, badge, desktop = triple
    return {"toast": toast, "badge": badge, "desktop": desktop}


@pytest.mark.parametrize(
    "level,success,silent",
    list(itertools.product(LEVELS_IN_APP, (True, False), (True, False))),
)
def test_in_app_flags_every_combination(level, success, silent):
    if silent:
        expected = _flags((False, False, False))
    else:
        expected = _flags(_TABLE[level][0 if success else 1])
    assert in_app_flags(level, success=success, silent=silent) == expected


@pytest.mark.parametrize("level", LEVELS_IN_APP)
@pytest.mark.parametrize("success", [True, False])
def test_desktop_only_for_toast_eligible_failures(level, success):
    flags = in_app_flags(level, success=success, silent=False)
    if flags["desktop"]:
        assert flags["toast"] is True
        assert success is False


@pytest.mark.parametrize("level", ["", "never", "ALL", None, 1, "failure"])
def test_in_app_flags_rejects_unknown_levels(level):
    with pytest.raises(ValueError):
        in_app_flags(level, success=True, silent=False)


def test_in_app_flags_returns_independent_dicts():
    first = in_app_flags("all", success=True, silent=False)
    first["toast"] = False
    assert in_app_flags("all", success=True, silent=False)["toast"] is True
    silent = in_app_flags("all", success=True, silent=True)
    silent["badge"] = True
    assert in_app_flags("all", success=True, silent=True)["badge"] is False


# ---------------------------------------------------------------------------
# combine_flags (job level AND viewer level)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_level,viewer_level,success",
    list(itertools.product(LEVELS_IN_APP, LEVELS_IN_APP, (True, False))),
)
def test_combine_is_the_and_of_job_and_viewer(job_level, viewer_level, success):
    job_flags = in_app_flags(job_level, success=success, silent=False)
    viewer_flags = in_app_flags(viewer_level, success=success, silent=False)
    combined = combine_flags(job_flags, viewer_flags)
    assert combined == {key: job_flags[key] and viewer_flags[key] for key in job_flags}


def test_viewer_mute_only_lowers():
    job_flags = in_app_flags("quiet", success=False, silent=False)
    viewer_flags = in_app_flags("all", success=False, silent=False)
    assert combine_flags(job_flags, viewer_flags) == job_flags


def test_combine_single_set_is_identity():
    flags = {"toast": True, "badge": False, "desktop": True}
    assert combine_flags(flags) == flags


def test_combine_three_sets():
    everything = {"toast": True, "badge": True, "desktop": True}
    no_desktop = {"toast": True, "badge": True, "desktop": False}
    no_toast = {"toast": False, "badge": True, "desktop": True}
    assert combine_flags(everything, no_desktop, no_toast) == {
        "toast": False,
        "badge": True,
        "desktop": False,
    }


def test_combine_missing_key_counts_as_false():
    assert combine_flags({"toast": True, "badge": True, "desktop": True}, {"toast": True}) == {
        "toast": True,
        "badge": False,
        "desktop": False,
    }


def test_combine_nothing_fails_closed():
    assert combine_flags() == {"toast": False, "badge": False, "desktop": False}


def test_combine_returns_plain_bools_and_ignores_extra_keys():
    combined = combine_flags({"toast": 1, "badge": "yes", "desktop": 0, "silent": True})
    assert combined == {"toast": True, "badge": True, "desktop": False}
    assert all(type(value) is bool for value in combined.values())


# ---------------------------------------------------------------------------
# legacy_toast_value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "in_app,expected", [("all", True), ("failures", True), ("quiet", False), ("off", False)]
)
@pytest.mark.parametrize("delivery", LEVELS_DELIVERY)
def test_legacy_toast_value(in_app, delivery, expected):
    assert legacy_toast_value({"in_app": in_app, "delivery": delivery}) is expected


def test_legacy_toast_value_fills_defaults():
    assert legacy_toast_value({"delivery": "off"}) is True


@pytest.mark.parametrize("value", [None, "all", {"in_app": "never"}, {"x": 1}])
def test_legacy_toast_value_rejects_invalid(value):
    with pytest.raises(ValueError):
        legacy_toast_value(value)


@pytest.mark.parametrize("in_app", LEVELS_IN_APP)
def test_legacy_toast_value_round_trips_through_resolve(in_app):
    """Older clients that read only toast_notifications see the same on/off."""
    notify = {"in_app": in_app, "delivery": "all"}
    legacy_job = {"toast_notifications": legacy_toast_value(notify)}
    legacy_level = resolve_notify(legacy_job)["in_app"]
    toast_on_failure = in_app_flags(legacy_level, success=False, silent=False)["toast"]
    assert toast_on_failure is legacy_toast_value(notify)


# ---------------------------------------------------------------------------
# normalize_managed_by
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""])
def test_managed_by_defaults_to_client(value):
    assert normalize_managed_by(value) == "client"


@pytest.mark.parametrize("value", MANAGED_BY)
def test_managed_by_known_values(value):
    assert normalize_managed_by(value) == value


@pytest.mark.parametrize(
    "value", ["Synthwave", "SYNTHWAVE", " synthwave", "partner", "none", 0, False, True, [], {}]
)
def test_managed_by_rejects_anything_else(value):
    with pytest.raises(ValueError):
        normalize_managed_by(value)


# ---------------------------------------------------------------------------
# Module shape: stdlib only, loadable on its own (the WebUI byte copy)
# ---------------------------------------------------------------------------


def test_module_imports_only_the_standard_library():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in stdlib, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no package-relative imports"
            assert (node.module or "").split(".")[0] in stdlib, node.module


def test_module_loads_standalone_under_another_name():
    spec = importlib.util.spec_from_file_location("_notify_policy_standalone_copy", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.should_deliver({"notify": {"delivery": "failures"}}, success=True) == (
        False,
        "suppressed_muted",
    )
    assert module.resolve_notify({"toast_notifications": False})["in_app"] == "quiet"


def test_public_names_are_exported():
    for name in policy_module.__all__:
        assert hasattr(policy_module, name), name
