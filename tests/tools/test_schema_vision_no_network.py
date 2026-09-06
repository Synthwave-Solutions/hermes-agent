"""Schema wording does not probe providers; actual image routing still can."""
from types import SimpleNamespace
from agent import image_routing
from tools import browser_use_cli


def setup_route(monkeypatch, cfg):
    monkeypatch.setattr("agent.auxiliary_client._read_main_provider", lambda: "custom")
    monkeypatch.setattr("agent.auxiliary_client._read_main_model", lambda: "test-vision")
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(browser_use_cli, "_lightpanda_engine_in_use", lambda: False)


def test_cold_schema_does_not_probe_catalog_or_local_server(monkeypatch):
    setup_route(monkeypatch, {})
    calls = []
    def caps(*args, **kwargs):
        calls.append(kwargs["allow_network"])
        assert kwargs["allow_network"] is False
        return None
    monkeypatch.setattr("agent.models_dev.get_model_capabilities", caps)
    monkeypatch.setattr(image_routing, "_should_probe_ollama_vision", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no endpoint probe")))
    assert browser_use_cli._description_header() == browser_use_cli._HEADER_BASE + browser_use_cli._HEADER_AUTO_VISION
    assert calls == [False]


def test_actual_image_routing_keeps_network_capability_resolution(monkeypatch):
    calls = []
    def caps(*args, **kwargs):
        calls.append(kwargs["allow_network"])
        return SimpleNamespace(supports_vision=True)
    monkeypatch.setattr("agent.models_dev.get_model_capabilities", caps)
    assert image_routing.decide_image_input_mode("custom", "test-vision", {}) == "native"
    assert calls == [True]


def test_cached_known_vision_preserves_schema_description(monkeypatch):
    setup_route(monkeypatch, {"model": {"supports_vision": True}, "agent": {"image_input_mode": "native"}})
    assert browser_use_cli._description_header() == browser_use_cli._HEADER_BASE + browser_use_cli._HEADER_VISION
