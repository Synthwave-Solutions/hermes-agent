"""Actual agent init probes only Ollama when checking its num_ctx safeguard.

All profiles and probe caches are temporary. Only external SDK construction
and tool inventory are replaced; the initializer, compressor and Ollama
metadata path run normally against intercepted HTTP.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import json
from pathlib import Path
import threading
import time
from unittest.mock import MagicMock

import httpx
import pytest
import requests

from agent import model_metadata as metadata
import run_agent


GATEWAY = "http://fixture-gateway:8080/v1"
MODEL = "fixture-model"


@pytest.fixture
def build_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(run_agent, "OpenAI", MagicMock())
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    monkeypatch.setattr(metadata, "_endpoint_probe_path_cache", {})
    monkeypatch.setattr(metadata, "_LOCAL_CTX_PROBE_CACHE", {})
    monkeypatch.setattr(metadata, "_ollama_probe_misses", {})

    def build(
        *,
        base_url=GATEWAY,
        requested="custom:fixture",
        model=MODEL,
        context=131072,
        num_ctx=None,
    ):
        cfg = {
            "model": {
                "default": model,
                "provider": requested,
                "base_url": base_url,
                "context_length": context,
            },
            "agent": {"environment_probe": False},
        }
        if num_ctx is not None:
            cfg["model"]["ollama_num_ctx"] = num_ctx
        Path(tmp_path, "config.yaml").write_text(json.dumps(cfg))
        return run_agent.AIAgent(
            model=model,
            provider="custom",
            requested_provider=requested,
            base_url=base_url,
            api_key="fixture-key",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
        )

    return build


@pytest.fixture
def intercepted_http(monkeypatch):
    calls = []
    behavior = {
        "ollama": False,
        "vllm": False,
        "num_ctx": None,
        "entered": None,
        "release": None,
    }

    def request(_client, method, url, **kwargs):
        calls.append((method.upper(), str(url), kwargs))
        if behavior["entered"] is not None and behavior.get(
            "gate_url", str(url)
        ) == str(url):
            behavior["entered"].set()
            behavior["release"].wait(min(_client.timeout.read, 5))
            raise TimeoutError("synthetic hung local gateway")
        if not behavior["ollama"] and not behavior["vllm"]:
            raise TimeoutError("synthetic unavailable local gateway")
        data = {}
        status = 404
        if behavior["ollama"] and str(url).endswith("/api/tags"):
            status, data = 200, {"models": []}
        elif behavior["ollama"] and str(url).endswith("/api/show"):
            status = 200
            params = (
                "" if behavior["num_ctx"] is None else f"num_ctx {behavior['num_ctx']}"
            )
            data = {
                "parameters": params,
                "model_info": {"llama.context_length": 262144},
            }
        elif behavior["vllm"] and str(url).endswith("/version"):
            status, data = 200, {"version": "fixture"}
        return httpx.Response(status, json=data)

    monkeypatch.setattr(httpx.Client, "request", request)
    monkeypatch.setattr(requests.sessions.Session, "request", request)
    return calls, behavior


@pytest.mark.parametrize(
    "base_url", [GATEWAY, "http://127.0.0.1:8080/v1", "http://100.77.1.2:8080/v1"]
)
def test_unknown_custom_gateway_only_checks_ollama_tags(
    build_agent, intercepted_http, base_url
):
    calls, _ = intercepted_http
    agent = build_agent(base_url=base_url)
    assert agent._ollama_num_ctx is None
    assert agent.context_compressor.context_length == 131072
    assert [(m, u) for m, u, _ in calls] == [("GET", base_url[:-3] + "/api/tags")]


def test_unknown_custom_gateway_pays_only_one_bounded_probe(
    build_agent, intercepted_http
):
    calls, behavior = intercepted_http
    behavior.update(entered=threading.Event(), release=threading.Event())
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(build_agent)
    try:
        try:
            agent = future.result(timeout=3.5)
        except FutureTimeout:
            pytest.fail(
                "agent init paid multiple metadata timeouts instead of one Ollama probe"
            )
        assert agent._ollama_num_ctx is None
        assert behavior["entered"].is_set()
        assert [(m, u) for m, u, _ in calls] == [
            ("GET", "http://fixture-gateway:8080/api/tags")
        ]
    finally:
        behavior["release"].set()
        executor.shutdown(wait=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # Custom Ollama behind a reverse proxy on an arbitrary port.
        {"requested": "ollama"},
        {"requested": "local"},
        {"model": "local:fixture-model"},
        {"base_url": "http://127.0.0.1:11434/v1"},
    ],
)
def test_explicit_local_ollama_detection_and_vram_cap_are_preserved(
    build_agent, intercepted_http, kwargs
):
    calls, behavior = intercepted_http
    behavior["ollama"] = True
    agent = build_agent(**kwargs)
    assert agent._ollama_num_ctx == 131072
    assert agent.context_compressor.context_length == 131072
    assert [u.rsplit("/", 2)[-2:] for m, u, _ in calls if m == "GET"] == [
        ["api", "tags"]
    ]
    assert any(
        method == "POST" and url.endswith("/api/show") for method, url, _ in calls
    )


@pytest.mark.parametrize("cache_kind", ["memory", "disk"])
def test_cached_ollama_on_custom_port_keeps_live_show_and_modelfile_cap(
    build_agent, intercepted_http, cache_kind
):
    calls, behavior = intercepted_http
    behavior.update(ollama=True, num_ctx=65536)
    if cache_kind == "memory":
        metadata._endpoint_probe_path_cache["http://fixture-gateway:8080"] = (
            "ollama",
            time.monotonic(),
        )
    else:
        metadata._local_probe_disk_put(
            "server_type", "http://fixture-gateway:8080", "ollama"
        )
    agent = build_agent()
    assert agent._ollama_num_ctx == 65536
    assert agent.context_compressor.context_length == 65536
    assert agent.context_compressor.threshold_tokens < 65536
    assert [(method, url) for method, url, _ in calls] == [
        ("POST", "http://fixture-gateway:8080/api/show")
    ]


@pytest.mark.parametrize("server_type", ["vllm", "lm-studio", "llamacpp", None])
def test_known_non_ollama_server_never_receives_show(
    build_agent, intercepted_http, server_type
):
    calls, behavior = intercepted_http
    behavior["ollama"] = True
    metadata._endpoint_probe_path_cache["http://fixture-gateway:8080"] = (
        server_type,
        time.monotonic(),
    )
    agent = build_agent()
    assert agent._ollama_num_ctx is None
    assert calls == []


def test_expired_ollama_cache_does_not_identify_replaced_gateway(
    build_agent, intercepted_http
):
    calls, _ = intercepted_http
    metadata._endpoint_probe_path_cache["http://fixture-gateway:8080"] = (
        "ollama",
        time.monotonic() - 100000,
    )
    agent = build_agent()
    assert agent._ollama_num_ctx is None
    assert [(m, u) for m, u, _ in calls] == [
        ("GET", "http://fixture-gateway:8080/api/tags")
    ]


def test_explicit_num_ctx_override_keeps_existing_compressor_clamp(
    build_agent, intercepted_http
):
    calls, _ = intercepted_http
    agent = build_agent(num_ctx=65536)
    assert agent._ollama_num_ctx == 65536
    assert agent.context_compressor.context_length == 65536
    assert calls == []


def test_targeted_failure_does_not_poison_generic_server_detection(
    build_agent, intercepted_http
):
    calls, behavior = intercepted_http
    agent = build_agent()
    assert agent._ollama_num_ctx is None
    assert "http://fixture-gateway:8080" not in metadata._endpoint_probe_path_cache
    assert (
        metadata._local_probe_disk_get("server_type", "http://fixture-gateway:8080")
        is None
    )
    calls.clear()
    behavior["vllm"] = True
    assert metadata.detect_local_server_type(GATEWAY) == "vllm"
    assert any(u.endswith("/api/v1/models") for _, u, _ in calls)
    assert calls[-1][1].endswith("/version")


def test_repeated_agent_init_reuses_targeted_miss(build_agent, intercepted_http):
    calls, _ = intercepted_http
    first = build_agent()
    second = build_agent()
    assert first._ollama_num_ctx is second._ollama_num_ctx is None
    assert [(m, u) for m, u, _ in calls] == [
        ("GET", "http://fixture-gateway:8080/api/tags")
    ]


def test_targeted_miss_normalizes_localhost_and_v1(build_agent, intercepted_http):
    calls, _ = intercepted_http
    build_agent(base_url="http://localhost:8080/v1")
    assert (
        metadata.query_ollama_num_ctx(
            MODEL, "http://127.0.0.1:8080", api_key="fixture-key"
        )
        is None
    )
    assert len(calls) == 1


def test_targeted_miss_does_not_mask_new_credentials(build_agent, intercepted_http):
    calls, behavior = intercepted_http
    build_agent()
    behavior["ollama"] = True
    assert (
        metadata.query_ollama_num_ctx(MODEL, GATEWAY, api_key="rotated-fixture-key")
        == 262144
    )
    assert [m for m, _, _ in calls] == ["GET", "GET", "POST"]


def test_targeted_miss_does_not_cross_profile(
    build_agent, intercepted_http, monkeypatch, tmp_path
):
    calls, behavior = intercepted_http
    build_agent()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-profile"))
    behavior["ollama"] = True
    assert (
        metadata.query_ollama_num_ctx(MODEL, GATEWAY, api_key="fixture-key") == 262144
    )
    assert [m for m, _, _ in calls] == ["GET", "GET", "POST"]


def test_targeted_miss_expires_and_allows_recovery(
    build_agent, intercepted_http, monkeypatch
):
    calls, behavior = intercepted_http
    build_agent()
    old = metadata.time.monotonic
    monkeypatch.setattr(
        metadata.time,
        "monotonic",
        lambda: old() + metadata._ENDPOINT_PROBE_FAILURE_TTL_SECONDS + 1,
    )
    behavior["ollama"] = True
    assert (
        metadata.query_ollama_num_ctx(MODEL, GATEWAY, api_key="fixture-key") == 262144
    )
    assert [m for m, _, _ in calls] == ["GET", "GET", "POST"]


def test_new_generic_positive_overrides_targeted_miss(build_agent, intercepted_http):
    calls, behavior = intercepted_http
    build_agent()
    behavior["ollama"] = True
    assert metadata.detect_local_server_type(GATEWAY, api_key="fixture-key") == "ollama"
    calls.clear()
    assert (
        metadata.query_ollama_num_ctx(MODEL, GATEWAY, api_key="fixture-key") == 262144
    )
    assert [m for m, _, _ in calls] == ["POST"]


def test_targeted_miss_cache_is_bounded_and_contains_only_digests(
    build_agent, intercepted_http, monkeypatch
):
    calls, _ = intercepted_http
    monkeypatch.setattr(metadata, "_OLLAMA_PROBE_MISS_MAX_SIZE", 3)
    build_agent()
    for port in range(8100, 8104):
        assert (
            metadata.query_ollama_num_ctx(
                MODEL, f"http://fixture-gateway:{port}/v1", api_key="fixture-key"
            )
            is None
        )
    assert len(metadata._ollama_probe_misses) == 3
    assert all(
        len(key) == 64 and set(key) <= set("0123456789abcdef")
        for key in metadata._ollama_probe_misses
    )
    before = len(calls)
    assert metadata.query_ollama_num_ctx(MODEL, GATEWAY, api_key="fixture-key") is None
    assert len(calls) == before + 1


def test_unrelated_probe_is_not_blocked_by_cache_lock_during_http(
    build_agent, intercepted_http
):
    _, behavior = intercepted_http
    behavior.update(
        entered=threading.Event(),
        release=threading.Event(),
        gate_url="http://fixture-gateway:8080/api/tags",
    )
    executor = ThreadPoolExecutor(max_workers=2)
    first = executor.submit(
        metadata.query_ollama_num_ctx, MODEL, GATEWAY, "fixture-key"
    )
    try:
        assert behavior["entered"].wait(2)
        second = executor.submit(
            metadata.query_ollama_num_ctx,
            MODEL,
            "http://other-gateway:8080/v1",
            "fixture-key",
        )
        assert second.result(timeout=2) is None
        assert not first.done()
    finally:
        behavior["release"].set()
        executor.shutdown(wait=True)


def test_concurrent_targeted_misses_keep_cache_bound(
    build_agent, intercepted_http, monkeypatch
):
    monkeypatch.setattr(metadata, "_OLLAMA_PROBE_MISS_MAX_SIZE", 3)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(
                metadata.query_ollama_num_ctx, MODEL, GATEWAY, f"fixture-key-{i}"
            )
            for i in range(18)
        ]
        assert all(future.result(timeout=5) is None for future in futures)
    assert len(metadata._ollama_probe_misses) == 3
