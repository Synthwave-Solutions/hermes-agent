"""Scheduler wake gate: behavior contracts."""
from __future__ import annotations

import tests.cron.test_scheduler as _support

pytest_plugins = ["tests.cron.test_scheduler"]


class TestRunJobWakeGate:
    """Integration tests for run_job wake-gate short-circuit."""

    @_support.pytest.fixture(autouse=True)
    def _stub_runtime_provider(self):
        """Stub ``resolve_runtime_provider`` for wake-gate tests.

        ``run_job`` resolves the runtime provider BEFORE constructing
        ``AIAgent``, so these tests must mock ``resolve_runtime_provider``
        in addition to ``AIAgent`` — otherwise in a hermetic CI env (no
        API keys), the resolver raises and the test fails before the
        patched AIAgent is ever reached.
        """
        fake_runtime = {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
            "source": "stub",
            "requested_provider": None,
        }
        with _support.patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value=fake_runtime,
        ):
            yield

    def _make_job(self, name="wake-gate-test", script="check.py"):
        """Minimal valid cron job dict for run_job."""
        return {
            "id": f"job_{name}",
            "name": name,
            "prompt": "Do a thing",
            "schedule": "*/5 * * * *",
            "script": script,
        }

    def test_wake_false_skips_agent_and_returns_silent(self, caplog, tmp_path):
        """When _run_job_script output ends with {wakeAgent: false}, the agent
        is not invoked and run_job returns the SILENT marker so delivery is
        suppressed."""
        from cron import scheduler
        from cron import scheduler_script as sched_script

        with _support.patch.object(sched_script, "_run_job_script",
                          return_value=(True, '{"wakeAgent": false}')), \
             _support.patch("run_agent.AIAgent") as agent_cls:
            success, _doc, final, err = _support._run_owned_job(self._make_job(), tmp_path)

        assert success is True
        assert err is None
        assert final == _support.SILENT_MARKER
        agent_cls.assert_not_called()

    def test_wake_true_runs_agent_with_injected_output(self, tmp_path):
        """When the script returns {wakeAgent: true, data: ...}, the agent is
        invoked and the data line still shows up in the prompt."""
        from cron import scheduler
        from cron import scheduler_script as sched_script

        script_output = '{"wakeAgent": true, "data": {"new": 3}}'
        agent = _support.MagicMock()
        agent.run_conversation = _support.MagicMock(return_value={
            "final_response": "ok", "messages": []
        })
        with _support.patch.object(sched_script, "_run_job_script",
                          return_value=(True, script_output)), \
             _support.patch("run_agent.AIAgent", return_value=agent) as agent_cls:
            success, _doc, _final, err = _support._run_owned_job(self._make_job(), tmp_path)

        agent_cls.assert_called_once()
        # The script output should be visible in the prompt passed to
        # run_conversation.
        call_kwargs = agent.run_conversation.call_args
        prompt_arg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("user_message", "")
        assert script_output in prompt_arg
        assert success is True
        assert err is None
