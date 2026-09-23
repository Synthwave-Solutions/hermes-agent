"""Endpoint metadata isolation and coalescing through real local HTTP clients."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent import model_metadata as metadata
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


MODEL = "fixture-model"


@pytest.fixture(autouse=True)
def isolated_caches(monkeypatch):
    for name in ("_endpoint_model_metadata_cache", "_endpoint_model_metadata_cache_time",
                 "_endpoint_probe_path_cache", "_endpoint_blackhole_cache",
                 "_LOCAL_CTX_PROBE_CACHE", "_lmstudio_probe_misses"):
        monkeypatch.setattr(metadata, name, {})
    monkeypatch.setattr(metadata, "_endpoint_model_metadata_inflight", {})


@contextmanager
def local_catalog(*, serialized_delay=0):
    state = {"requests": [], "contexts": {"key-A": 65536, "key-B": 131072},
             "status": 200, "entered": threading.Event(), "release": threading.Event(),
             "block": False}
    counter_lock, service_lock = threading.Condition(), threading.Lock()
    state["counter_lock"] = counter_lock

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            key = self.headers.get("Authorization", "").removeprefix("Bearer ")
            with counter_lock:
                state["requests"].append((self.path, key))
                counter_lock.notify_all()
            if self.path != "/v1/models":
                status, payload = 404, {}
            else:
                state["entered"].set()
                if state["block"]:
                    state["release"].wait()  # every caller releases in finally
                # A serialized metadata backend makes duplicate requests measurable.
                with service_lock:
                    time.sleep(serialized_delay)
                    status = state["status"]
                    payload = {"data": [{"id": MODEL,
                                         "context_length": state["contexts"].get(key, 32768),
                                         "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        state["release"].set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def wait_for_background_refresh(timeout=5.0):
    deadline = time.monotonic() + timeout
    while metadata._endpoint_model_metadata_inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not metadata._endpoint_model_metadata_inflight, "background refresh did not finish"


def fetch_in_profile(home, url, key="key-A", **kwargs):
    token = set_hermes_home_override(home)
    try:
        return metadata.fetch_endpoint_model_metadata(url, api_key=key, **kwargs)
    finally:
        reset_hermes_home_override(token)


def test_concurrent_same_scope_uses_one_real_http_request(tmp_path, record_property):
    with local_catalog(serialized_delay=0.15) as (url, state):
        barrier = threading.Barrier(6)

        def fetch():
            barrier.wait(timeout=5)
            return fetch_in_profile(tmp_path, url)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: fetch(), range(6)))
        elapsed = time.monotonic() - started
        count = sum(path == "/v1/models" for path, _ in state["requests"])
        print(json.dumps({"case": "six_parallel_native_fetches", "models_requests": count,
                          "elapsed_seconds": round(elapsed, 4)}))
        record_property("models_requests", count)
        record_property("elapsed_seconds", round(elapsed, 4))
        assert all(result[MODEL]["context_length"] == 65536 for result in results)
        assert count == 1


def test_credential_rotation_cannot_reuse_other_credentials_catalog(tmp_path):
    with local_catalog() as (url, state):
        first = fetch_in_profile(tmp_path, url, "key-A")
        second = fetch_in_profile(tmp_path, url, "key-B")
        assert first[MODEL]["context_length"] == 65536
        assert second[MODEL]["context_length"] == 131072
        assert [key for path, key in state["requests"] if path == "/v1/models"] == ["key-A", "key-B"]


def test_profile_switch_cannot_reuse_other_profiles_snapshot(tmp_path):
    with local_catalog() as (url, state):
        assert fetch_in_profile(tmp_path / "A", url)[MODEL]["context_length"] == 65536
        state["contexts"]["key-A"] = 98304
        assert fetch_in_profile(tmp_path / "B", url)[MODEL]["context_length"] == 98304
        assert fetch_in_profile(tmp_path / "A", url)[MODEL]["context_length"] == 65536


def test_cached_only_does_not_disclose_other_scope_or_probe(tmp_path):
    with local_catalog() as (url, state):
        expected = fetch_in_profile(tmp_path / "A", url)
        count = len(state["requests"])
        assert fetch_in_profile(tmp_path / "A", url, cached_only=True) == expected
        assert fetch_in_profile(tmp_path / "A", url, "key-B", cached_only=True) == {}
        assert fetch_in_profile(tmp_path / "B", url, cached_only=True) == {}
        assert len(state["requests"]) == count


@pytest.mark.parametrize("separation", ["profile", "credential", "endpoint"])
def test_slow_probe_does_not_block_another_identity(tmp_path, separation):
    with local_catalog() as (url, state), local_catalog() as (other_url, _):
        state["block"] = True
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(fetch_in_profile, tmp_path / "A", url)
            try:
                assert state["entered"].wait(3)
                home = tmp_path / ("B" if separation == "profile" else "A")
                key = "key-B" if separation == "credential" else "key-A"
                second_url = other_url if separation == "endpoint" else url
                second = executor.submit(fetch_in_profile, home, second_url, key)
                if separation == "endpoint":
                    assert second.result(timeout=3)[MODEL]["context_length"] == 65536
                else:
                    with state["counter_lock"]:
                        assert state["counter_lock"].wait_for(
                            lambda: sum(p == "/v1/models" for p, _ in state["requests"]) == 2,
                            timeout=3,
                        ), "a foreign identity was blocked by the slow probe"
            finally:
                state["release"].set()
            assert first.result(timeout=3)[MODEL]["context_length"] == 65536
            assert second.result(timeout=3)[MODEL]["context_length"] == (131072 if key == "key-B" else 65536)


def test_cached_only_never_waits_for_inflight_metadata(tmp_path):
    with local_catalog() as (url, state), ThreadPoolExecutor(max_workers=2) as executor:
        state["block"] = True
        first = executor.submit(fetch_in_profile, tmp_path, url)
        try:
            assert state["entered"].wait(3)
            snapshot = executor.submit(fetch_in_profile, tmp_path, url, cached_only=True)
            assert snapshot.result(timeout=3) == {}
        finally:
            state["release"].set()
        assert first.result(timeout=3)[MODEL]["context_length"] == 65536


def test_follower_timeout_does_not_cancel_or_duplicate_leader(monkeypatch, tmp_path):
    monkeypatch.setattr(metadata, "_ENDPOINT_MODEL_FETCH_WAIT_SECONDS", 0.02)
    with local_catalog() as (url, state), ThreadPoolExecutor(max_workers=2) as executor:
        state["block"] = True
        first = executor.submit(fetch_in_profile, tmp_path, url)
        try:
            assert state["entered"].wait(3)
            follower = executor.submit(fetch_in_profile, tmp_path, url)
            assert follower.result(timeout=3) == {}
            assert sum(p == "/v1/models" for p, _ in state["requests"]) == 1
        finally:
            state["release"].set()
        result = first.result(timeout=3)
        assert fetch_in_profile(tmp_path, url, cached_only=True) == result


def observe_follower(monkeypatch):
    """Observe entry to native Event.wait, avoiding scheduler/timing assertions."""
    entered = threading.Event()
    original = metadata._EndpointMetadataFetch

    class ObservedFetch(original):
        def __init__(self):
            super().__init__()
            wait = self.done.wait

            def observed_wait(timeout=None):
                entered.set()
                return wait(timeout)

            self.done.wait = observed_wait

    monkeypatch.setattr(metadata, "_EndpointMetadataFetch", ObservedFetch)
    return entered


def test_concurrent_forced_refresh_shares_fresh_probe(monkeypatch, tmp_path):
    joined = observe_follower(monkeypatch)
    with local_catalog() as (url, state), ThreadPoolExecutor(max_workers=2) as executor:
        original = fetch_in_profile(tmp_path, url)
        state["contexts"]["key-A"] = 98304
        state["block"] = True
        state["entered"].clear()
        first = executor.submit(fetch_in_profile, tmp_path, url, force_refresh=True)
        try:
            assert state["entered"].wait(3)
            second = executor.submit(fetch_in_profile, tmp_path, url, force_refresh=True)
            assert joined.wait(3)
            assert fetch_in_profile(tmp_path, url, cached_only=True) == original
        finally:
            state["release"].set()
        assert first.result(timeout=3) == second.result(timeout=3)
        assert first.result()[MODEL]["context_length"] == 98304
        assert sum(p == "/v1/models" for p, _ in state["requests"]) == 2


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_aborted_leader_releases_followers_and_allows_later_retry(monkeypatch, tmp_path, failure):
    joined = observe_follower(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = metadata._fetch_endpoint_model_metadata_uncached

    def abort(*_args):
        entered.set()
        release.wait()  # released by the test's finally, including assertion failure
        raise failure("synthetic interruption")

    monkeypatch.setattr(metadata, "_fetch_endpoint_model_metadata_uncached", abort)
    with local_catalog() as (url, _), ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(fetch_in_profile, tmp_path, url)
        try:
            assert entered.wait(3)
            second = executor.submit(fetch_in_profile, tmp_path, url)
            assert joined.wait(3)
        finally:
            release.set()
        with pytest.raises(failure):
            first.result(timeout=3)
        assert second.result(timeout=3) == {}
        assert fetch_in_profile(tmp_path, url, cached_only=True) == {}
        monkeypatch.setattr(metadata, "_fetch_endpoint_model_metadata_uncached", original)
        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 65536
        assert not metadata._endpoint_model_metadata_inflight


@pytest.mark.parametrize("status", [401, 403])
def test_negative_snapshot_remains_scoped_and_force_refresh_recovers(tmp_path, status):
    with local_catalog() as (url, state):
        state["status"] = status
        assert fetch_in_profile(tmp_path, url) == {}
        count = len(state["requests"])
        assert fetch_in_profile(tmp_path, url) == {}
        assert len(state["requests"]) == count
        state["status"] = 200
        assert fetch_in_profile(tmp_path, url, "key-B")[MODEL]["context_length"] == 131072
        assert fetch_in_profile(tmp_path, url, force_refresh=True)[MODEL]["context_length"] == 65536


def test_metadata_cache_expires_and_is_bounded_without_raw_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(metadata, "_ENDPOINT_MODEL_CACHE_MAX_SIZE", 2)
    with local_catalog() as (url, state):
        for key in ("key-A", "key-B", "key-C"):
            fetch_in_profile(tmp_path, url, key)
        assert fetch_in_profile(tmp_path, url, cached_only=True) == {}
        assert fetch_in_profile(tmp_path, url, "key-B", cached_only=True)[MODEL]["context_length"] == 131072
        assert len(metadata._endpoint_model_metadata_cache) == 2
        assert len(metadata._endpoint_model_metadata_cache_time) == 2
        assert all(len(key) == 64 and "key-" not in key and str(tmp_path) not in key
                   for key in metadata._endpoint_model_metadata_cache)
        now = time.time()
        monkeypatch.setattr(metadata.time, "time", lambda: now + metadata._ENDPOINT_MODEL_CACHE_TTL + 1)
        state["contexts"]["key-B"] = 98304
        assert fetch_in_profile(tmp_path, url, "key-B", cached_only=True) == {}
        # Stale-while-revalidate (SYNTHWAVE fork): the expired snapshot is served
        # at once and a background refresh replaces it.
        assert fetch_in_profile(tmp_path, url, "key-B")[MODEL]["context_length"] == 131072
        wait_for_background_refresh()
        assert fetch_in_profile(tmp_path, url, "key-B")[MODEL]["context_length"] == 98304


def test_real_agent_context_and_usage_accounting_share_scoped_snapshot(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    import run_agent
    from agent.usage_pricing import estimate_usage_cost, normalize_usage

    with local_catalog() as (url, state):
        (tmp_path / "config.yaml").write_text(json.dumps({
            "model": {"default": "fixture-default", "provider": "custom:fixture",
                      "base_url": url, "context_length": 200000},
            "agent": {"environment_probe": False},
        }))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(run_agent, "OpenAI", MagicMock())
        monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [])
        monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
        agent = run_agent.AIAgent(model=MODEL, provider="custom", requested_provider="custom:fixture",
                                 base_url=url, api_key="key-A", quiet_mode=True,
                                 skip_context_files=True, skip_memory=True, skip_background_review=True)
        assert agent._config_context_length is None
        assert agent.context_compressor.context_length == 65536
        count = len(state["requests"])
        result = estimate_usage_cost(MODEL, normalize_usage({"prompt_tokens": 100, "completion_tokens": 10}),
                                     provider="custom", base_url=url, api_key="key-A", cached_only=True)
        assert result.amount_usd is not None and result.amount_usd > 0
        assert len(state["requests"]) == count


# ── Stale-while-revalidate (SYNTHWAVE fork) ─────────────────────────────────


def _expire(monkeypatch, age):
    now = time.time()
    monkeypatch.setattr(metadata.time, "time", lambda: now + age)


def test_expired_snapshot_is_served_while_refresh_is_blocked(monkeypatch, tmp_path):
    with local_catalog() as (url, state):
        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 65536
        _expire(monkeypatch, metadata._ENDPOINT_MODEL_CACHE_TTL + 1)
        state["contexts"]["key-A"] = 262144
        state["block"] = True
        state["entered"].clear()

        started = time.monotonic()
        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 65536
        assert time.monotonic() - started < 1.0
        assert state["entered"].wait(2), "no background refresh was started"
        # A second caller while the refresh is still blocked also gets the stale
        # snapshot and does not start another request.
        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 65536
        # One catalog request for the cold fetch, one for the single refresh.
        assert [path for path, _key in state["requests"]].count("/v1/models") == 2

        state["release"].set()
        wait_for_background_refresh()
        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 262144


def test_failed_background_refresh_keeps_the_good_snapshot(monkeypatch, tmp_path):
    with local_catalog() as (url, state):
        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 65536
        _expire(monkeypatch, metadata._ENDPOINT_MODEL_CACHE_TTL + 1)
        state["status"] = 500

        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 65536
        wait_for_background_refresh()
        requests_after_refresh = len(state["requests"])
        # Still the good snapshot, and it counts as fresh again for a short
        # retry window instead of refetching on every call.
        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 65536
        assert len(state["requests"]) == requests_after_refresh


def test_snapshot_older_than_stale_window_waits_for_fresh_data(monkeypatch, tmp_path):
    with local_catalog() as (url, state):
        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 65536
        _expire(monkeypatch, metadata._ENDPOINT_MODEL_STALE_MAX_AGE + 1)
        state["contexts"]["key-A"] = 262144
        assert fetch_in_profile(tmp_path, url)[MODEL]["context_length"] == 262144

