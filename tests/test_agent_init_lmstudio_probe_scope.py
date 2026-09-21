"""Real initializer/context resolution with synthetic metadata HTTP only."""
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import httpx
import pytest
import requests

import run_agent
from agent import model_metadata as metadata
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


BASE = "http://127.0.0.1:18888/v1"
MODEL = "fixture-active"


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("_endpoint_probe_path_cache", "_LOCAL_CTX_PROBE_CACHE", "_ollama_probe_misses",
                 "_endpoint_model_metadata_cache", "_endpoint_model_metadata_cache_time",
                 "_endpoint_blackhole_cache"):
        monkeypatch.setattr(metadata, name, {})
    if hasattr(metadata, "_lmstudio_probe_misses"):
        monkeypatch.setattr(metadata, "_lmstudio_probe_misses", {})
    calls = []
    behavior = {"lmstudio": False, "ollama": False, "rich": True}

    def request(_client, method, url, **kwargs):
        path = urlsplit(str(url)).path
        calls.append((method, path))
        payload, status = {}, 404
        if path == "/api/v1/models" and behavior["lmstudio"]:
            status = 200
            payload = {"models": [{"key": MODEL, "max_context_length": 262144,
                                   "loaded_instances": [{"config": {"context_length": 98304}}]}]}
        elif path == "/api/tags" and behavior["ollama"]:
            status, payload = 200, {"models": []}
        elif path == "/api/show" and behavior["ollama"]:
            status, payload = 200, {"parameters": "num_ctx 98304",
                                    "model_info": {"llama.context_length": 262144}}
        elif path == "/v1/models":
            status, payload = 200, {"data": [{"id": MODEL, "context_length": 131072}]
                                    if behavior["rich"] else []}
        return httpx.Response(status, json=payload, request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.Client, "request", request)
    monkeypatch.setattr(requests.sessions.Session, "request", request)
    return calls, behavior


def build(monkeypatch, tmp_path):
    cfg = {"model": {"default": "fixture-default", "provider": "custom:fixture",
                     "base_url": BASE, "context_length": 200000},
           "agent": {"environment_probe": False}}
    Path(tmp_path, "config.yaml").write_text(json.dumps(cfg))
    monkeypatch.setattr(run_agent, "OpenAI", MagicMock())
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    return run_agent.AIAgent(model=MODEL, provider="custom", requested_provider="custom:fixture",
                            base_url=BASE, api_key="fixture-key", quiet_mode=True,
                            skip_context_files=True, skip_memory=True, skip_background_review=True)


def test_real_alternate_model_constructor_avoids_unrelated_local_protocol_probes(monkeypatch, tmp_path, gateway):
    calls, _ = gateway
    agent = build(monkeypatch, tmp_path)
    assert agent._config_context_length is None  # default pin cannot cross model boundaries
    assert agent.context_compressor.context_length == 131072
    assert [p for _, p in calls] == ["/api/v1/models", "/v1/models", "/api/tags"]
    assert agent._ollama_num_ctx is None


def test_metadata_fetch_checks_only_lmstudio_before_rich_openai_listing(gateway):
    calls, _ = gateway
    assert metadata.fetch_endpoint_model_metadata(BASE)[MODEL]["context_length"] == 131072
    assert [p for _, p in calls] == ["/api/v1/models", "/v1/models"]


def test_actual_lmstudio_loaded_context_still_precedes_catalog_maximum(monkeypatch, tmp_path, gateway):
    calls, behavior = gateway
    behavior["lmstudio"] = True
    agent = build(monkeypatch, tmp_path)
    assert agent.context_compressor.context_length == 98304
    assert [p for _, p in calls] == ["/api/v1/models", "/api/v1/models"]


def test_metadata_miss_keeps_full_ollama_detection_and_runtime_num_ctx(gateway):
    calls, behavior = gateway
    behavior.update(rich=False, ollama=True)
    assert metadata.get_model_context_length(MODEL, BASE, provider="custom") == 98304
    assert ("GET", "/api/tags") in calls
    assert ("POST", "/api/show") in calls


def test_targeted_miss_does_not_poison_full_detector(gateway):
    calls, behavior = gateway
    assert metadata.detect_local_server_type(BASE, lmstudio_only=True) is None
    assert not metadata._endpoint_probe_path_cache
    behavior["ollama"] = True
    assert metadata.detect_local_server_type(BASE) == "ollama"
    assert ("GET", "/api/tags") in calls


def test_targeted_miss_is_profile_and_credential_scoped(monkeypatch, tmp_path, gateway):
    calls, _ = gateway
    for _ in range(2):
        assert metadata.detect_local_server_type(BASE, api_key="key-A", lmstudio_only=True) is None
    assert len(calls) == 1
    metadata.detect_local_server_type(BASE, api_key="key-B", lmstudio_only=True)
    assert len(calls) == 2
    token = set_hermes_home_override(tmp_path / "second-profile")
    try:
        metadata.detect_local_server_type(BASE, api_key="key-B", lmstudio_only=True)
    finally:
        reset_hermes_home_override(token)
    assert len(calls) == 3
    assert all("key-" not in key and str(tmp_path) not in key for key in metadata._lmstudio_probe_misses)


def test_prior_ollama_only_miss_does_not_suppress_lmstudio_probe(gateway):
    calls, behavior = gateway
    assert metadata.detect_local_server_type(BASE, ollama_only=True) is None
    behavior["lmstudio"] = True
    assert metadata.detect_local_server_type(BASE, lmstudio_only=True) == "lm-studio"
    assert [p for _, p in calls] == ["/api/tags", "/api/v1/models"]


def test_positive_memory_verdict_wins_over_targeted_miss(gateway):
    calls, _ = gateway
    assert metadata.detect_local_server_type(BASE, lmstudio_only=True) is None
    metadata._endpoint_probe_path_cache[BASE[:-3]] = ("lm-studio", time.monotonic())
    assert metadata.detect_local_server_type(BASE, lmstudio_only=True) == "lm-studio"
    assert len(calls) == 1


def test_positive_disk_verdict_wins_over_targeted_miss(gateway):
    calls, _ = gateway
    assert metadata.detect_local_server_type(BASE, lmstudio_only=True) is None
    metadata._local_probe_disk_put("server_type", BASE[:-3], "lm-studio")
    assert metadata.detect_local_server_type(BASE, lmstudio_only=True) == "lm-studio"
    assert len(calls) == 1


def test_unscoped_generic_negative_does_not_hide_targeted_authenticated_probe(gateway):
    calls, behavior = gateway
    metadata._endpoint_probe_path_cache[BASE[:-3]] = (None, time.monotonic())
    behavior["lmstudio"] = True
    assert metadata.detect_local_server_type(BASE, api_key="rotated", lmstudio_only=True) == "lm-studio"
    assert len(calls) == 1


def test_targeted_negative_cache_is_bounded_and_expires(monkeypatch, gateway):
    clock = [100.0]
    monkeypatch.setattr(metadata.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(metadata, "_LMSTUDIO_PROBE_MISS_MAX_SIZE", 2)
    for key in ("one", "two", "three"):
        metadata._lmstudio_probe_miss_seen(key, record=True)
    assert set(metadata._lmstudio_probe_misses) == {"two", "three"}
    clock[0] += metadata._ENDPOINT_PROBE_FAILURE_TTL_SECONDS + 1
    assert not metadata._lmstudio_probe_miss_seen("three")
    assert not metadata._lmstudio_probe_misses


def test_conflicting_targeted_modes_refuse_without_http(gateway):
    calls, _ = gateway
    with pytest.raises(ValueError):
        metadata.detect_local_server_type(BASE, ollama_only=True, lmstudio_only=True)
    assert not calls


def test_targeted_cache_lock_does_not_block_another_profile_http(monkeypatch, tmp_path, gateway):
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()

    def request(_client, method, url, **kwargs):
        from hermes_constants import get_hermes_home
        slot = int(get_hermes_home().name)
        entered[slot].set()
        assert release.wait(5), "fixture release was not signalled"
        return httpx.Response(404, request=httpx.Request(method, str(url)))

    monkeypatch.setattr(httpx.Client, "request", request)

    def detect(slot):
        token = set_hermes_home_override(tmp_path / str(slot))
        try:
            return metadata.detect_local_server_type(BASE, api_key="same-key", lmstudio_only=True)
        finally:
            reset_hermes_home_override(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(detect, slot) for slot in (0, 1)]
        try:
            assert all(event.wait(3) for event in entered)
        finally:
            release.set()
        assert [future.result(timeout=3) for future in futures] == [None, None]
    assert len(metadata._lmstudio_probe_misses) == 2
