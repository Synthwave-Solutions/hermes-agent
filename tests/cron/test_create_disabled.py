"""A paused duplicate must never be published as enabled, even briefly."""
from copy import deepcopy
import pytest
from cron import jobs


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(jobs, "_compute_provider_model_snapshots", lambda **kw: (None, None))


def test_disabled_creation_first_publish_is_paused_and_cannot_be_claimed(store, monkeypatch):
    published = []
    save = jobs.save_jobs
    def record(rows):
        published.append(deepcopy(rows))
        return save(rows)
    monkeypatch.setattr(jobs, "save_jobs", record)
    job = jobs.create_job(prompt="Paused duplicate", schedule="every 1h", enabled=False)
    assert len(published) == 1
    assert published[0][0]["enabled"] is False
    assert published[0][0]["state"] == "paused"
    assert published[0][0]["paused_at"]
    assert jobs.get_job(job["id"])["enabled"] is False
    assert job["id"] not in {row["id"] for row in jobs.get_due_jobs()}
    resumed = jobs.resume_job(job["id"])
    assert resumed["enabled"] is True
    assert resumed["state"] == "scheduled"


@pytest.mark.parametrize("value", ["false", 0, None, []])
def test_invalid_enabled_never_creates_a_job(store, value):
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        jobs.create_job(prompt="Invalid", schedule="every 1h", enabled=value)
    assert jobs.load_jobs() == []
