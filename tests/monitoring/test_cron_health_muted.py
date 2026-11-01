def test_muted_execution_is_visible_without_content():
    from agent.monitoring.cron_health import project_execution_event
    event = project_execution_event({"id":"private", "job_id":"secret-name", "source":"direct", "status":"completed"}, delivery_outcome="suppressed_muted").to_dict()
    assert event["delivery_outcome"] == "suppressed_muted"
    assert "secret-name" not in str(event)
