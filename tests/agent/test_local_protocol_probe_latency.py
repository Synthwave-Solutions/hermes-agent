"""Exercise the real cached-context reconciliation path with native HTTPX clients."""
import threading
import time

import httpx
import pytest

from agent import model_metadata as metadata


MODEL = "fixture-local-context"
URL = "http://127.0.0.1:43219/v1"


@pytest.fixture
def probes(monkeypatch):
    for name in ("_endpoint_probe_path_cache", "_endpoint_blackhole_cache", "_LOCAL_CTX_PROBE_CACHE",
                 "_lmstudio_probe_misses", "_ollama_probe_misses"):
        monkeypatch.setattr(metadata, name, {})
    original_client = httpx.Client

    def install(*, first="read_timeout", winner=None, earlier_connect_timeout=False):
        state = {"paths": [], "active": 0, "maximum": 0, "threads": set()}
        lock = threading.Lock()

        def handle(request):
            path = request.url.path
            assert request.headers["Authorization"] == "Bearer fixture-key"
            assert request.extensions["timeout"]["read"] == (3.0 if path in {
                "/api/show", "/v1/models/" + MODEL} else 2.0)
            with lock:
                state["paths"].append(path)
                state["threads"].add(threading.current_thread().name)
            if path == "/api/v1/models":
                if first == "read_timeout":
                    time.sleep(0.02)
                    raise httpx.ReadTimeout("synthetic optional endpoint timeout", request=request)
                if first == "connect_timeout":
                    raise httpx.ConnectTimeout("synthetic endpoint unreachable", request=request)
                return httpx.Response(200 if first == "lm-studio" else 404, json={})
            if path in {"/api/tags", "/v1/props", "/version"}:
                with lock:
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                try:
                    time.sleep(0.08 if path == "/api/tags" else 0.05)
                    if earlier_connect_timeout and path == "/api/tags":
                        raise httpx.ConnectTimeout("synthetic first-priority connection failure", request=request)
                    if winner is not None:
                        payloads = {"/api/tags": {"models": []},
                                    "/v1/props": {"default_generation_settings": {}},
                                    "/version": {"version": "fixture"}}
                        return httpx.Response(200, json=payloads[path])
                    raise httpx.ReadTimeout("synthetic optional endpoint timeout", request=request)
                finally:
                    with lock:
                        state["active"] -= 1
            if path == "/api/show":
                return httpx.Response(200, json={"parameters": "num_ctx 98304", "model_info": {"context_length": 999999}})
            if path == "/v1/models/" + MODEL:
                return httpx.Response(200, json={"id": MODEL, "context_length": 98304})
            raise AssertionError("Unexpected fixture request: " + path)

        transport = httpx.MockTransport(handle)
        monkeypatch.setattr(httpx, "Client", lambda *a, **kw: original_client(*a, transport=transport, **kw))
        return state

    return install


def test_existing_context_cache_reconciles_with_overlapped_optional_probes(probes, record_property):
    state = probes()
    metadata.save_context_length(MODEL, URL, 65536)
    before = time.monotonic()
    result = metadata.get_model_context_length(MODEL, URL, api_key="fixture-key", provider="custom")
    elapsed = time.monotonic() - before
    record_property("reconciliation_seconds", elapsed)
    record_property("maximum_simultaneous_optional_probes", state["maximum"])
    print({"reconciliation_seconds": elapsed, "maximum_simultaneous_optional_probes": state["maximum"]})
    assert result == 98304
    assert state["maximum"] == 3
    assert sorted(state["paths"]) == sorted(["/api/v1/models", "/api/tags", "/v1/props", "/version", "/v1/models/" + MODEL])
    assert not any(thread.name in state["threads"] and thread is not threading.current_thread()
                   for thread in threading.enumerate())


def test_parallel_responses_keep_ollama_priority_and_runtime_num_ctx(probes):
    state = probes(winner="all")
    metadata.save_context_length(MODEL, URL, 65536)
    assert metadata.get_model_context_length(MODEL, URL, api_key="fixture-key", provider="custom") == 98304
    assert state["maximum"] == 3
    assert "/api/show" in state["paths"]
    assert "/v1/models/" + MODEL not in state["paths"]


@pytest.mark.parametrize("first", ["lm-studio", "connect_timeout"])
def test_initial_native_success_or_connect_timeout_does_not_speculate(probes, first):
    state = probes(first=first)
    assert metadata.detect_local_server_type(URL, api_key="fixture-key") == ("lm-studio" if first == "lm-studio" else None)
    assert state["paths"] == ["/api/v1/models"]


@pytest.mark.parametrize("target", ["lmstudio_only", "ollama_only"])
def test_targeted_detection_keeps_one_probe(probes, target):
    state = probes()
    assert metadata.detect_local_server_type(URL, api_key="fixture-key", **{target: True}) is None
    assert state["paths"] == (["/api/v1/models"] if target == "lmstudio_only" else ["/api/tags"])


def test_connect_failure_preserves_priority_and_blackhole_guard(probes):
    state = probes(winner="all", earlier_connect_timeout=True)
    assert metadata.detect_local_server_type(URL, api_key="fixture-key") is None
    assert metadata._endpoint_blackholed(URL)
    assert state["active"] == 0
