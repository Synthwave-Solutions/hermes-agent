"""An explicit router validates its current window through its models catalog."""
import httpx
import pytest
import requests

from agent import model_metadata as metadata
from run_agent import AIAgent

MODEL = "codex/gpt-6-astra"
URL = "http://127.0.0.1:20128/v1"


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    for name in ("_LOCAL_CTX_PROBE_CACHE", "_endpoint_probe_path_cache",
                 "_endpoint_blackhole_cache", "_endpoint_model_metadata_cache",
                 "_endpoint_model_metadata_cache_time", "_endpoint_model_metadata_inflight"):
        monkeypatch.setattr(metadata, name, {})
    metadata.save_context_length(MODEL, URL, 65536)
    state = {"paths": [], "window": 98304, "elapsed": 0.0, "status": 200,
             "unexpected": [], "interrupt": False}

    def response(request):
        path = request.url.path
        state["paths"].append((path, request.headers.get("Authorization")))
        if state["interrupt"]:
            raise KeyboardInterrupt("fixture cancellation")
        if path == "/v1/models/" + MODEL:
            state["elapsed"] += 3.0
            raise httpx.ReadTimeout("unsupported detail fixture", request=request)
        if path != "/v1/models":
            state["unexpected"].append(path)
            raise AssertionError("Unexpected endpoint")
        state["elapsed"] += 0.1
        return httpx.Response(state["status"], json={"data": [
            {"id": MODEL, "context_length": state["window"]}]})

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
        raise AssertionError("No unaccounted network request")
    monkeypatch.setattr(requests.Session, "send", refuse_requests)
    monkeypatch.setattr("run_agent.get_tool_definitions", lambda *a, **k: [])
    monkeypatch.setattr("run_agent.check_toolset_requirements", lambda *a, **k: {})
    yield state
    assert not state["unexpected"]


def resolve(key="fixture-a"):
    return metadata.get_model_context_length(MODEL, URL, key,
        provider="custom", requested_provider="custom:omniroute")


def test_native_constructor_uses_catalog_without_unsupported_detail(endpoint):
    agent = AIAgent(model=MODEL, provider="custom", requested_provider="custom:omniroute",
        api_key="fixture-a", base_url=URL, api_mode="chat_completions", quiet_mode=True,
        skip_context_files=True, skip_memory=True, platform="webui",
        reasoning_config={"enabled": True, "effort": "high"})
    try:
        assert agent.context_compressor.context_length == 98304
        assert endpoint["paths"] == [("/v1/models", "Bearer fixture-a")]
        assert endpoint["elapsed"] == 0.1
        assert agent.model == MODEL and agent.reasoning_config["effort"] == "high"
        assert agent.requested_provider == "custom:omniroute"
    finally:
        agent.close()


def test_changed_subminimum_live_catalog_invalidates_stale_disk_window(endpoint):
    endpoint["window"] = 32768
    assert resolve() == 32768
    assert metadata.get_cached_context_length(MODEL, URL) is None


def test_probe_snapshot_isolated_by_current_credential(endpoint):
    assert resolve() == 98304
    endpoint["window"] = 196608
    assert resolve("fixture-b") == 196608
    assert endpoint["paths"] == [("/v1/models", "Bearer fixture-a"),
                                 ("/v1/models", "Bearer fixture-b")]


def test_probe_snapshot_isolated_by_current_profile(endpoint, monkeypatch, tmp_path):
    assert resolve() == 98304
    other = tmp_path / "other-profile"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    metadata.save_context_length(MODEL, URL, 65536)
    endpoint["window"] = 196608
    assert resolve() == 196608
    assert len(endpoint["paths"]) == 2


def test_fresh_probe_reused_but_existing_thirty_second_ttl_not_extended(endpoint):
    assert resolve() == resolve() == 98304
    assert len(endpoint["paths"]) == 1
    for key, (value, at) in metadata._LOCAL_CTX_PROBE_CACHE.copy().items():
        metadata._LOCAL_CTX_PROBE_CACHE[key] = (value, at - 31)
    endpoint["window"] = 131072
    assert resolve() == 131072
    assert len(endpoint["paths"]) == 2


def test_failed_catalog_keeps_existing_fallback_without_memoizing_failure(endpoint):
    endpoint["status"] = 503
    assert resolve() == 65536
    assert not metadata._LOCAL_CTX_PROBE_CACHE
    endpoint["status"] = 200
    assert resolve() == 98304


def test_explicit_window_still_avoids_all_network(endpoint):
    assert metadata.get_model_context_length(MODEL, URL, "fixture-a",
        provider="custom", requested_provider="custom:omniroute",
        config_context_length=196608) == 196608
    assert endpoint["paths"] == []


def test_external_cancellation_propagates_without_positive_cache(endpoint):
    endpoint["interrupt"] = True
    with pytest.raises(KeyboardInterrupt, match="fixture cancellation"):
        resolve()
    assert not metadata._LOCAL_CTX_PROBE_CACHE
