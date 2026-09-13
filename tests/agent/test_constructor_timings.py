"""Native construction timings are scoped, content-free and observation-only."""
import json
import logging
import threading

import httpx
import pytest
import requests

from agent import agent_init, model_metadata
from run_agent import AIAgent

SID = "4cefa3f06733"
MODEL = "codex/gpt-6-astra"
URL = "http://127.0.0.1:20128/v1"
PHASES = ["provider_setup", "plugin_discovery", "tool_definitions", "session_setup",
          "profile_setup", "context_setup", "context_hooks", "finalize"]


def observations(caplog):
    prefix = "agent_init_timing "
    return [json.loads(record.getMessage()[len(prefix):]) for record in caplog.records
            if record.getMessage().startswith(prefix)]


@pytest.fixture
def native(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="run_agent")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    for name in ("_endpoint_probe_path_cache", "_endpoint_blackhole_cache", "_LOCAL_CTX_PROBE_CACHE",
                 "_lmstudio_probe_misses", "_ollama_probe_misses", "_endpoint_model_metadata_cache",
                 "_endpoint_model_metadata_cache_time", "_endpoint_model_metadata_inflight"):
        monkeypatch.setattr(model_metadata, name, {})
    model_metadata.save_context_length(MODEL, URL, 65536)
    state = {"clock": 0.0, "unexpected": [], "paths": [], "tool_error": None}
    monkeypatch.setattr(agent_init.time, "perf_counter", lambda: state["clock"])

    def response(request):
        state["paths"].append(request.url.path)
        if request.url.path != "/v1/models/" + MODEL:
            state["unexpected"].append("httpx")
            raise AssertionError("Unexpected hermetic HTTP path")
        assert request.headers["Authorization"] == "Bearer fixture-private-key"
        state["clock"] += 3.0
        return httpx.Response(200, json={"id": MODEL, "context_length": 98304})

    sync, async_ = httpx.Client, httpx.AsyncClient
    class Sync(sync):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(response)
            super().__init__(*args, **kwargs)
    class Async(async_):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(response)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(httpx, "Client", Sync)
    monkeypatch.setattr(httpx, "AsyncClient", Async)
    def refuse_requests(*args, **kwargs):
        state["unexpected"].append("requests")
        raise AssertionError("No external request permitted")
    monkeypatch.setattr(requests.Session, "send", refuse_requests)

    def tools(*args, **kwargs):
        state["clock"] += 5.0
        if state["tool_error"]:
            raise state["tool_error"]
        return []
    monkeypatch.setattr("run_agent.get_tool_definitions", tools)
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda *args, **kwargs: {})
    yield state
    assert not state["unexpected"], "Even a swallowed request must fail this hermetic fixture"


def create(**kwargs):
    return AIAgent(model=MODEL, provider="custom", requested_provider="custom:omniroute",
                   api_key="fixture-private-key", base_url=URL, api_mode="chat_completions",
                   quiet_mode=True, skip_context_files=True, skip_memory=True,
                   reasoning_config={"enabled": True, "effort": "high"},
                   session_id=SID, platform="webui", **kwargs)


def test_native_constructor_reports_actual_phase_cost_without_route_change(native, caplog):
    agent = create()
    try:
        assert agent.model == MODEL and agent.requested_provider == "custom:omniroute"
        assert agent.reasoning_config["effort"] == "high"
        assert agent.context_compressor.context_length == 98304
        assert native["paths"] == ["/v1/models/" + MODEL]
        rows = observations(caplog)
        assert len(rows) == 1
        row = rows[0]
        assert row["session_id"] == SID and row["outcome"] == "completed"
        assert [item["stage"] for item in row["phases"]] == PHASES
        costs = {item["stage"]: item["duration_ms"] for item in row["phases"]}
        assert costs["tool_definitions"] == 5000.0 and costs["context_setup"] == 3000.0
        assert sum(costs.values()) == 8000.0
        assert all(item["outcome"] == "completed" for item in row["phases"])
        encoded = json.dumps(row)
        for secret in (MODEL, URL, "fixture-private-key", "high", "context_length", "HERMES_HOME"):
            assert secret not in encoded
    finally:
        agent.close()


def test_native_constructor_failure_keeps_exception_and_last_phase(native, caplog):
    error = RuntimeError("fixture-private-failure")
    native["tool_error"] = error
    with pytest.raises(RuntimeError) as caught:
        create()
    assert caught.value is error
    rows = observations(caplog)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "raised"
    assert rows[0]["phases"][-1] == {"stage": "tool_definitions", "outcome": "raised", "duration_ms": 5000.0}
    assert "fixture-private-failure" not in json.dumps(rows)


@pytest.mark.parametrize("platform,sid", [("cli", SID), ("webui", None), ("webui", "private\ntext"),
                                         ("webui", "../../private"), ("webui", SID.upper())])
def test_unidentified_or_non_webui_construction_is_not_observed(caplog, platform, sid):
    caplog.set_level(logging.INFO, logger="run_agent")
    @agent_init._trace_constructor
    def construct(agent, **kwargs):
        agent_init._constructor_phase("tool_definitions")
        return "unchanged"
    assert construct(object(), platform=platform, session_id=sid) == "unchanged"
    assert observations(caplog) == []


def test_cancellation_and_broken_logger_do_not_replace_original_error(monkeypatch):
    cancelled = KeyboardInterrupt()
    @agent_init._trace_constructor
    def construct(agent, **kwargs):
        agent_init._constructor_phase("context_setup")
        raise cancelled
    def broken(*args, **kwargs):
        raise OSError("fixture-private-logger-error")
    monkeypatch.setattr(agent_init.logger, "info", broken)
    with pytest.raises(KeyboardInterrupt) as caught:
        construct(object(), platform="webui", session_id=SID)
    assert caught.value is cancelled
    assert agent_init._constructor_trace.get() is None


def test_nested_constructor_restores_parent_and_rejects_unknown_stage(caplog):
    caplog.set_level(logging.INFO, logger="run_agent")
    @agent_init._trace_constructor
    def child(agent, **kwargs):
        agent_init._constructor_phase("context_setup")
    @agent_init._trace_constructor
    def parent(agent, **kwargs):
        agent_init._constructor_phase("tool_definitions")
        child(object(), platform="webui", session_id="abcdef012345")
        agent_init._constructor_phase("fixture-private-stage")
        agent_init._constructor_phase("finalize")
    parent(object(), platform="webui", session_id=SID)
    rows = {row["session_id"]: row for row in observations(caplog)}
    assert [p["stage"] for p in rows[SID]["phases"]] == ["provider_setup", "tool_definitions", "finalize"]
    assert [p["stage"] for p in rows["abcdef012345"]["phases"]] == ["provider_setup", "context_setup"]
    assert "fixture-private-stage" not in json.dumps(rows)
    assert agent_init._constructor_trace.get() is None


def test_overlapping_workers_keep_independent_phase_sequences(caplog):
    caplog.set_level(logging.INFO, logger="run_agent")
    barrier = threading.Barrier(2)
    @agent_init._trace_constructor
    def construct(agent, *, stage, **kwargs):
        agent_init._constructor_phase(stage)
        barrier.wait(timeout=3)
    failures = []
    def worker(sid, stage):
        try:
            construct(object(), platform="webui", session_id=sid, stage=stage)
        except BaseException as error:
            failures.append(error)
    threads = [threading.Thread(target=worker, args=(SID, "tool_definitions")),
               threading.Thread(target=worker, args=("abcdef012345", "context_setup"))]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=5)
    assert not failures and not any(thread.is_alive() for thread in threads)
    rows = {row["session_id"]: row for row in observations(caplog)}
    assert [p["stage"] for p in rows[SID]["phases"]] == ["provider_setup", "tool_definitions"]
    assert [p["stage"] for p in rows["abcdef012345"]["phases"]] == ["provider_setup", "context_setup"]
