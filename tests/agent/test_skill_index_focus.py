"""The always-on skill index stays proportional to what people actually open.

The property that must never break: a demoted category is demoted, not hidden.
This module only ever returns category names for the renderer to render
names-only; if it ever became a way to make a skill unreachable, an agent would
silently lose a capability it still has.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent import skill_index_focus as focus  # noqa: E402


USAGE_MODE = {"skills": {"index_mode": "usage"}}


def _skill(root: Path, category: str, name: str, declared: str | None = None) -> None:
    d = root / category / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {declared or name}\ndescription: does {name}\n---\n\nbody\n",
        encoding="utf-8",
    )


def _usage(root: Path, entries: dict) -> None:
    (root / ".usage.json").write_text(json.dumps(entries), encoding="utf-8")


@pytest.fixture
def catalogue(tmp_path):
    root = tmp_path / "skills"
    # A big dead collection, a big live one, and a small one.
    for i in range(40):
        _skill(root, "bulk", f"bulk-{i}")
    for i in range(30):
        _skill(root, "daily", f"daily-{i}")
    for i in range(5):
        _skill(root, "tiny", f"tiny-{i}")
    used = {f"daily-{i}": {"use_count": 5, "view_count": 5} for i in range(20)}
    used["bulk-0"] = {"use_count": 1, "view_count": 1}
    _usage(root, used)
    return root


class TestMode:
    def test_off_by_default(self, catalogue):
        assert focus.compact_categories(catalogue, config={}) == frozenset()

    def test_full_is_explicitly_off(self, catalogue):
        assert focus.compact_categories(catalogue, config={"skills": {"index_mode": "full"}}) == frozenset()

    def test_aliases_turn_it_on(self):
        for value in ("usage", "used", "lean", "smart", "USAGE"):
            assert focus.index_mode({"skills": {"index_mode": value}}) == "usage"

    def test_an_unknown_value_stays_off(self):
        assert focus.index_mode({"skills": {"index_mode": "banana"}}) == "full"


class TestTheRule:
    def test_a_large_unused_category_is_demoted(self, catalogue):
        assert "bulk" in focus.compact_categories(catalogue, config=USAGE_MODE)

    def test_a_used_category_is_kept(self, catalogue):
        assert "daily" not in focus.compact_categories(catalogue, config=USAGE_MODE)

    def test_a_small_category_is_never_demoted(self, catalogue):
        # Too few skills for a share to mean anything, and too little to gain.
        assert "tiny" not in focus.compact_categories(catalogue, config=USAGE_MODE)

    def test_a_handful_of_recent_hits_does_not_save_a_huge_dead_category(self, tmp_path):
        # The regression that made this proportional: 9 recent views among 863
        # skills kept the whole collection in full.
        root = tmp_path / "skills"
        for i in range(200):
            _skill(root, "bulk", f"b-{i}")
        _usage(root, {f"b-{i}": {"view_count": 1, "last_viewed_at": "2999-01-01T00:00:00+00:00"} for i in range(4)})
        assert "bulk" in focus.compact_categories(root, config=USAGE_MODE)

    def test_broad_recent_activity_does_save_a_category(self, tmp_path):
        root = tmp_path / "skills"
        for i in range(40):
            _skill(root, "active", f"a-{i}")
        _usage(root, {f"a-{i}": {"view_count": 1, "last_viewed_at": "2999-01-01T00:00:00+00:00"} for i in range(20)})
        assert "active" not in focus.compact_categories(root, config=USAGE_MODE)

    def test_a_pinned_skill_protects_its_category(self, tmp_path):
        root = tmp_path / "skills"
        for i in range(40):
            _skill(root, "bulk", f"b-{i}")
        _usage(root, {"b-0": {"pinned": True}})
        assert "bulk" not in focus.compact_categories(root, config=USAGE_MODE)

    def test_a_named_category_is_always_kept_in_full(self, catalogue):
        config = {"skills": {"index_mode": "usage", "index_always_full": ["bulk"]}}
        assert focus.compact_categories(catalogue, config=config) == frozenset()

    def test_thresholds_are_configurable(self, catalogue):
        # Demand near-total usage and even the live category falls below it.
        config = {"skills": {"index_mode": "usage", "index_used_share": 0.99}}
        assert "daily" in focus.compact_categories(catalogue, config=config)


class TestMatchingIdentifiers:
    def test_usage_recorded_under_the_declared_name_still_counts(self, tmp_path):
        # The usage file keys on a skill's declared name, which is often not
        # its directory name. Matching only one would report a live category
        # as dead and demote something people rely on.
        root = tmp_path / "skills"
        for i in range(40):
            _skill(root, "live", f"dir-{i}", declared=f"Nice Name {i}")
        _usage(root, {f"Nice Name {i}": {"use_count": 3} for i in range(25)})
        assert focus.compact_categories(root, config=USAGE_MODE) == frozenset()

    def test_usage_recorded_under_the_directory_name_still_counts(self, tmp_path):
        root = tmp_path / "skills"
        for i in range(40):
            _skill(root, "live", f"dir-{i}", declared=f"Nice Name {i}")
        _usage(root, {f"dir-{i}": {"use_count": 3} for i in range(25)})
        assert focus.compact_categories(root, config=USAGE_MODE) == frozenset()


class TestFailsOpen:
    def test_no_usage_file_renders_the_full_index(self, tmp_path):
        root = tmp_path / "skills"
        for i in range(40):
            _skill(root, "bulk", f"b-{i}")
        assert focus.compact_categories(root, config=USAGE_MODE) == frozenset()

    def test_an_unreadable_usage_file_renders_the_full_index(self, tmp_path):
        root = tmp_path / "skills"
        for i in range(40):
            _skill(root, "bulk", f"b-{i}")
        (root / ".usage.json").write_text("{not json", encoding="utf-8")
        assert focus.compact_categories(root, config=USAGE_MODE) == frozenset()

    def test_a_missing_directory_renders_the_full_index(self, tmp_path):
        assert focus.compact_categories(tmp_path / "nope", config=USAGE_MODE) == frozenset()

    def test_it_only_ever_returns_category_names(self, catalogue):
        # Guards the invariant: this module cannot hide an individual skill,
        # it can only ask the renderer to drop a category's descriptions.
        result = focus.compact_categories(catalogue, config=USAGE_MODE)
        assert isinstance(result, frozenset)
        assert all(isinstance(name, str) and "/" not in name for name in result)
