from __future__ import annotations

import json

from hermes_cli.dashboard_governance.audit import append_audit_event, read_audit_events


def test_audit_log_redacts_secret_like_values(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("hermes_cli.dashboard_governance.audit._audit_file", lambda: path)

    append_audit_event(
        "deny",
        subject_email="User@Example.test",
        path="/api/config",
        method="GET",
        reason="permission_not_allowed",
        extra={
            "api_key": "sk-secret-value",
            "nested": {"Authorization": "Bearer abc123"},
        },
    )

    raw = path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in raw
    assert "Bearer abc123" not in raw
    row = json.loads(raw)
    assert row["event"] == "deny"
    assert row["subject_email_hash"]
    assert row["path"] == "/api/config"
    assert row["extra"]["api_key"] == "[REDACTED]"
    assert row["extra"]["nested"]["Authorization"] == "[REDACTED]"


def test_read_audit_events_returns_newest_first_and_limits(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr("hermes_cli.dashboard_governance.audit._audit_file", lambda: path)

    append_audit_event("deny", path="/api/one")
    append_audit_event("policy_change", path="/api/two")
    append_audit_event("usage_cap", path="/api/three")

    events = read_audit_events(limit=2)

    assert [event["event"] for event in events] == ["usage_cap", "policy_change"]
    assert [event["path"] for event in events] == ["/api/three", "/api/two"]
