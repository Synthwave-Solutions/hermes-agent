"""Tool availability must not wait for vision metadata; image dispatch still can.

Exercise the real requirements -> auxiliary resolver -> image-routing chain,
using only synthetic credentials, a temporary profile, and intercepted HTTP.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
import json
from pathlib import Path
import threading
import time

import httpx
import pytest
import requests

from agent import auxiliary_client as aux
from agent import image_routing, model_metadata, models_dev
from tools.vision_tools import check_vision_requirements


MODEL = "fixture-uncatalogued-vision"
LOCAL_URL = "http://127.0.0.1:9/v1"


@pytest.fixture
def isolated_route(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", 0)
    monkeypatch.setattr(models_dev, "_models_dev_retry_after", 0)
    monkeypatch.setattr(models_dev, "_models_dev_refresh_in_flight", False)
    monkeypatch.setattr(models_dev, "_load_disk_cache", lambda: {})
    monkeypatch.setattr(models_dev, "_disk_cache_age_seconds", lambda: None)
    monkeypatch.setattr(model_metadata, "_endpoint_probe_path_cache", {})
    monkeypatch.setattr(model_metadata, "_local_probe_disk_get", lambda *a: None)
    # set_runtime_main maintains legacy mirrors as well as context-local state.
    # Preserve them with monkeypatch so no synthetic route leaks to later tests.
    for field in aux._MAIN_RUNTIME_FIELDS:
        name = "_RUNTIME_MAIN_" + field.upper()
        monkeypatch.setattr(aux, name, "")
    monkeypatch.setattr(
        aux,
        "_RUNTIME_MAIN_COMPAT_SNAPSHOT",
        tuple("" for _ in aux._MAIN_RUNTIME_FIELDS),
    )

    @contextmanager
    def route(provider="custom", *, capability=None):
        cfg = {
            "model": {"provider": provider, "default": MODEL},
            "auxiliary": {"vision": {"provider": "auto"}},
        }
        if capability is not None:
            cfg["model"]["supports_vision"] = capability
        Path(tmp_path, "config.yaml").write_text(json.dumps(cfg))
        base_url = LOCAL_URL if provider == "custom" else "https://openrouter.ai/api/v1"
        if provider == "openrouter":
            monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key")
        token = aux.set_runtime_main(
            provider, MODEL, base_url=base_url, api_key="fixture-key"
        )
        try:
            yield
        finally:
            aux.reset_runtime_main(token)

    return route


@pytest.fixture
def intercepted_http(monkeypatch):
    calls = []
    gate = {"entered": None, "release": None}

    def blocked_request(_client, method, url, *args, **kwargs):
        calls.append((method, str(url)))
        if gate["entered"] is not None:
            gate["entered"].set()
            # Bounded even if the calling test is interrupted.
            gate["release"].wait(5)
        raise TimeoutError("synthetic metadata service unavailable")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked_request)
    monkeypatch.setattr(httpx.Client, "request", blocked_request)
    return calls, gate


@pytest.mark.parametrize("provider", ["custom", "openrouter"])
def test_availability_does_not_contact_failing_metadata(
    isolated_route, intercepted_http, provider
):
    calls, _ = intercepted_http
    with isolated_route(provider):
        assert check_vision_requirements() is True
    assert calls == [], "tool availability attempted metadata HTTP"


@pytest.mark.parametrize("provider", ["custom", "openrouter"])
def test_availability_does_not_wait_for_hung_metadata(
    isolated_route, intercepted_http, provider
):
    calls, gate = intercepted_http
    gate.update(entered=threading.Event(), release=threading.Event())

    def check():
        # Thread-local probe mode and context-local runtime must both be real.
        with isolated_route(provider):
            return check_vision_requirements()

    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(check)
    try:
        try:
            available = future.result(timeout=2)
        except FutureTimeout:
            pytest.fail("availability blocked on a synthetic hung metadata request")
        assert available is True
        assert not gate["entered"].is_set()
        assert calls == []
    finally:
        gate["release"].set()
        executor.shutdown(wait=True)


@pytest.mark.parametrize("provider", ["custom", "openrouter"])
def test_runtime_resolution_still_uses_live_capability_lookup(
    isolated_route, intercepted_http, provider
):
    calls, _ = intercepted_http
    with isolated_route(provider):
        resolved_provider, client, model = aux.resolve_vision_provider_client()
        try:
            assert client is not None
            assert not isinstance(client, aux._AuxProbeClientStub)
            assert model == MODEL
            assert resolved_provider == provider
            assert calls, "real image resolution must retain live capability lookup"
        finally:
            if client is not None and callable(getattr(client, "close", None)):
                client.close()


def test_image_dispatch_rechecks_unknown_availability_and_respects_live_false(
    isolated_route,
    intercepted_http,
    monkeypatch,
):
    calls, _ = intercepted_http
    with isolated_route("openrouter"):
        assert check_vision_requirements() is True
        assert calls == []

        def live_catalog(_client, method, url, **kwargs):
            calls.append((method, str(url)))
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({
                "openrouter": {"models": {MODEL: {"attachment": False}}},
            }).encode()
            return response

        monkeypatch.setattr(requests.sessions.Session, "request", live_catalog)
        assert image_routing.decide_image_input_mode("openrouter", MODEL, {}) == "text"
        assert len(calls) == 1


@pytest.mark.parametrize("capability", [False, True])
def test_configured_capability_is_preserved(
    isolated_route, intercepted_http, capability
):
    calls, _ = intercepted_http
    with isolated_route(capability=capability):
        assert check_vision_requirements() is capability
    assert calls == []


@pytest.mark.parametrize("capability", [False, True])
def test_cached_catalog_capability_is_preserved(
    isolated_route, intercepted_http, monkeypatch, capability
):
    calls, _ = intercepted_http
    monkeypatch.setattr(
        models_dev,
        "_models_dev_cache",
        {
            "openrouter": {"models": {MODEL: {"attachment": capability}}},
        },
    )
    # An expired cache must remain useful without even a background refresh.
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", time.time() - 100000)
    monkeypatch.setattr(
        models_dev,
        "_start_background_refresh_models_dev",
        lambda: calls.append(("refresh", "models.dev")),
    )
    with isolated_route("openrouter"):
        with aux.aux_probe_mode():
            assert aux._main_model_supports_vision("openrouter", MODEL) is capability
    assert calls == []


def test_probe_context_restores_after_exception_and_nested_scope(
    isolated_route, intercepted_http
):
    calls, _ = intercepted_http
    with isolated_route():
        with pytest.raises(RuntimeError, match="fixture"):
            with aux.aux_probe_mode():
                with aux.aux_probe_mode():
                    assert aux._main_model_supports_vision("custom", MODEL) is True
                assert aux._main_model_supports_vision("custom", MODEL) is True
                assert calls == []
                raise RuntimeError("fixture")
        assert aux._main_model_supports_vision("custom", MODEL) is True
        assert calls


def test_probe_mode_does_not_disable_another_threads_runtime_lookup(
    isolated_route, intercepted_http
):
    calls, _ = intercepted_http

    def runtime_check():
        with isolated_route():
            return aux._main_model_supports_vision("custom", MODEL)

    with aux.aux_probe_mode(), ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(runtime_check).result(timeout=2) is True
    assert calls
