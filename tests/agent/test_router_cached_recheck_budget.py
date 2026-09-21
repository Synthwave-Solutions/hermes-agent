"""A known router window survives a slow optional check without delaying startup."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import requests

from agent import model_metadata as metadata

MODEL = "codex/gpt-6-astra"
KEY = "fixture-router-key"


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")
    for name in ("_LOCAL_CTX_PROBE_CACHE", "_endpoint_probe_path_cache",
                 "_endpoint_blackhole_cache", "_endpoint_model_metadata_cache",
                 "_endpoint_model_metadata_cache_time", "_endpoint_model_metadata_inflight"):
        monkeypatch.setattr(metadata, name, {})
    # Force the existing cold catalog-miss fallback without outbound requests.
    monkeypatch.setattr(metadata, "_resolve_endpoint_context_length", lambda *a, **k: None)
    monkeypatch.setattr(metadata, "detect_local_server_type", lambda *a, **k: None)
    unexpected = []
    def refuse(*args, **kwargs):
        unexpected.append(True)
        raise AssertionError("No external request allowed")
    monkeypatch.setattr(requests.Session, "send", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    yield tmp_path
    assert not unexpected


def resolve(url, requested="custom:omniroute", **kwargs):
    return metadata.get_model_context_length(MODEL, url, KEY,
        provider="custom", requested_provider=requested, **kwargs)


def test_real_slow_get_retains_window_promptly_then_recovers(isolated, monkeypatch):
    release = threading.Event()
    arrived = threading.Event()
    paths = []
    slow = [True]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            paths.append((self.path, self.headers.get("Authorization")))
            if slow[0]:
                arrived.set()
                release.wait(8)
                return
            payload = json.dumps({"data": [{"id": MODEL, "context_length": 98304}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    url = "http://127.0.0.1:%s/v1" % server.server_port
    send = httpx.Client.send
    def only_fixture(client, request, **kwargs):
        assert request.url.host == "127.0.0.1" and request.url.port == server.server_port
        return send(client, request, **kwargs)
    monkeypatch.setattr(httpx.Client, "send", only_fixture)
    metadata.save_context_length(MODEL, url, 65536)
    before = (isolated / "context_length_cache.yaml").read_bytes()
    try:
        started = time.monotonic()
        assert resolve(url) == 65536
        elapsed = time.monotonic() - started
        assert arrived.is_set()
        assert elapsed < 2.0, "Optional router recheck must not spend the old 3s read timeout"
        assert (isolated / "context_length_cache.yaml").read_bytes() == before
        assert not metadata._LOCAL_CTX_PROBE_CACHE
        assert not metadata._endpoint_blackhole_cache
        release.set()
        slow[0] = False
        assert resolve(url) == 98304
        assert metadata.get_cached_context_length(MODEL, url) == 98304
        assert resolve(url) == 98304
        assert paths == [("/v1/models", "Bearer " + KEY)] * 2
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(3)
        assert not worker.is_alive()


def install_transport(monkeypatch, handler):
    original = httpx.Client
    class Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(httpx, "Client", Client)


@pytest.mark.parametrize("cached,requested,expected_timeout", [
    (65536, "custom:omniroute", 0.5),
    (None, "custom:omniroute", 3.0),
    (65536, "custom:native-local", 3.0),
])
def test_only_known_router_window_gets_short_timeout(isolated, monkeypatch, cached, requested, expected_timeout):
    url = "http://127.0.0.1:20128/v1"
    if cached:
        metadata.save_context_length(MODEL, url, cached)
    seen = []
    def response(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"context_length": 98304,
            "data": [{"id": MODEL, "context_length": 98304}]})
    install_transport(monkeypatch, response)
    assert resolve(url, requested=requested) == 98304
    assert seen == [dict(connect=expected_timeout, read=expected_timeout,
                         write=expected_timeout, pool=expected_timeout)]


def test_short_connect_timeout_cannot_blackhole_next_cold_lookup(isolated, monkeypatch):
    url = "http://127.0.0.1:20128/v1"
    metadata.save_context_length(MODEL, url, 65536)
    seen = []
    def response(request):
        seen.append(request.extensions["timeout"]["connect"])
        if len(seen) == 1:
            raise httpx.ConnectTimeout("fixture timeout", request=request)
        return httpx.Response(200, json={"data": [{"id": MODEL, "context_length": 98304}]})
    install_transport(monkeypatch, response)
    assert resolve(url) == 65536
    assert not metadata._endpoint_blackhole_cache
    metadata._invalidate_cached_context_length(MODEL, url)
    assert resolve(url) == 98304
    assert seen == [0.5, 3.0]


def test_cold_connect_timeout_still_uses_existing_blackhole_policy(isolated, monkeypatch):
    def response(request):
        assert request.extensions["timeout"]["connect"] == 3.0
        raise httpx.ConnectTimeout("fixture timeout", request=request)
    install_transport(monkeypatch, response)
    metadata._query_local_context_length_uncached(MODEL, "http://127.0.0.1:20128/v1", KEY,
                                                 native_protocol_probes=False)
    assert metadata._endpoint_blackhole_cache


def test_explicit_context_override_never_probes(isolated, monkeypatch):
    install_transport(monkeypatch, lambda request: pytest.fail("Explicit override must win"))
    url = "http://127.0.0.1:20128/v1"
    metadata.save_context_length(MODEL, url, 65536)
    assert resolve(url, config_context_length=196608) == 196608


def test_prompt_lower_live_window_still_invalidates_old_cache(isolated, monkeypatch):
    url = "http://127.0.0.1:20128/v1"
    metadata.save_context_length(MODEL, url, 65536)
    install_transport(monkeypatch, lambda request: httpx.Response(200,
        json={"data": [{"id": MODEL, "context_length": 32768}]}))
    assert resolve(url) == 32768
    assert metadata.get_cached_context_length(MODEL, url) is None


def test_cancellation_propagates_without_failure_cache(isolated, monkeypatch):
    url = "http://127.0.0.1:20128/v1"
    metadata.save_context_length(MODEL, url, 65536)
    def cancelled(request):
        raise KeyboardInterrupt()
    install_transport(monkeypatch, cancelled)
    with pytest.raises(KeyboardInterrupt):
        resolve(url)
    assert not metadata._LOCAL_CTX_PROBE_CACHE
    assert not metadata._endpoint_blackhole_cache
