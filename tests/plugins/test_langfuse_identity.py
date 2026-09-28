"""Identity and redaction in the engine's Langfuse traces (SynthPulse R2).

Every trace carries the user (the pseudonym, or ``module:<name>``), the
profile, and the organisation, installation and module of the
``model_gateway`` block; content capture defaults to ``metadata`` (D57);
``sanitized`` mode masks personal data as well as secrets.
"""

from __future__ import annotations

import contextlib
import importlib
import sys

import pytest
import yaml

SALT = "sp-test-salt-v1"
PERSON = "Alice.Example@Acme.Example "
PSEUDONYM = "u_00ce9e9f4c1f"


def _fresh_plugin():
    sys.modules.pop("plugins.observability.langfuse", None)
    return importlib.import_module("plugins.observability.langfuse")


def _write_config(config):
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    from agent import model_gateway

    model_gateway.reset_for_tests()


class _Span:
    def __init__(self):
        self.trace_updates = []

    def update(self, **kw):
        pass

    def end(self, **kw):
        pass

    def update_trace(self, **kw):
        self.trace_updates.append(kw)

    def start_observation(self, **kw):
        return _Span()


class _RootCM:
    def __init__(self, sink):
        self.span = _Span()
        sink.append(self.span)

    def __enter__(self):
        return self.span

    def __exit__(self, *exc):
        return False


class _Client:
    def __init__(self):
        self.roots = []
        self.observations = []

    def create_trace_id(self, seed=None):
        return f"trace::{seed}"

    def start_as_current_observation(self, **kw):
        self.observations.append(kw)
        return _RootCM(self.roots)

    def flush(self):
        pass


@pytest.fixture
def traced(monkeypatch, tmp_path):
    mod = _fresh_plugin()
    client = _Client()
    propagated = []

    @contextlib.contextmanager
    def _propagate(**kw):
        propagated.append(kw)
        yield

    monkeypatch.setattr(mod, "_get_langfuse", lambda: client)
    monkeypatch.setattr(mod, "propagate_attributes", _propagate)
    monkeypatch.setattr(mod, "_end_observation", lambda *a, **k: None)
    mod._TRACE_STATE.clear()
    salt = tmp_path / "org.salt"
    salt.write_text(SALT, encoding="utf-8")
    monkeypatch.setenv("SP_AUDIT_ORG_SALT_FILE", str(salt))
    monkeypatch.setenv("HERMES_LANGFUSE_ENV", "hq-vps")
    return mod, client, propagated


def _start_trace(mod, turn="turn-1"):
    mod.on_pre_llm_request(
        task_id="t1", session_id="s1", model="m", provider="custom", api_mode="chat_completions",
        api_call_count=1, request_messages=[{"role": "user", "content": "hi"}], turn_id=turn,
    )


def test_every_trace_carries_user_profile_org_module_and_installation(traced):
    mod, client, propagated = traced
    _write_config({"model_gateway": {"base_url": "http://gw/v1", "org_id": "acme", "installation_id": "acme/prod",
                                     "module": "engine", "principal": PERSON}})
    _start_trace(mod)
    assert propagated, "propagate_attributes not used"
    attrs = propagated[0]
    assert attrs["user_id"] == PSEUDONYM
    assert {"org:acme", "inst:acme/prod", "mod:engine", "profile:default", "env:hq-vps"} <= set(attrs["tags"])
    assert attrs["metadata"]["org_id"] == "acme"
    assert attrs["metadata"]["installation_id"] == "acme/prod"
    assert attrs["metadata"]["profile"] == "default"
    root_metadata = client.observations[0]["metadata"]
    assert root_metadata["user"] == PSEUDONYM and root_metadata["module"] == "engine"
    update = client.roots[0].trace_updates[0]
    assert update["user_id"] == PSEUDONYM and "org:acme" in update["tags"]
    assert PERSON.strip().lower() not in str(propagated).lower()


def test_service_traffic_is_traced_as_the_module(traced):
    mod, _client, propagated = traced
    _write_config({"model_gateway": {"org_id": "acme", "module": "cron"}})
    _start_trace(mod, turn="turn-2")
    assert propagated[0]["user_id"] == "module:cron"
    assert "mod:cron" in propagated[0]["tags"]


def test_capture_defaults_to_metadata(monkeypatch):
    mod = _fresh_plugin()
    monkeypatch.delenv("HERMES_LANGFUSE_CAPTURE", raising=False)
    assert mod._capture_mode() == "metadata"
    captured = mod._capture_content("my IBAN is NL91 ABNA 0417 1643 00")
    assert captured == {"omitted": True, "type": "text", "chars": 33}


def test_sanitized_mode_masks_personal_data(monkeypatch):
    mod = _fresh_plugin()
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", "sanitized")
    text = (
        "mail alice.example@acme.example, call +31 6 12345678 or 0612345678, "
        "IBAN NL91 ABNA 0417 1643 00, card 4111 1111 1111 1111, BSN 111222333, "
        "order 123456789, invoice 2026-09-28, build 1.2.3"
    )
    out = mod._capture_content(text)
    for masked in ("[email]", "[phone]", "[iban]", "[card]", "[bsn]"):
        assert masked in out
    for raw in ("alice.example@acme.example", "+31 6 12345678", "0612345678", "NL91", "4111", "111222333"):
        assert raw not in out
    # Numbers that fail the card and BSN checksums stay readable.
    assert "123456789" in out and "2026-09-28" in out and "1.2.3" in out


def test_full_mode_keeps_content(monkeypatch):
    mod = _fresh_plugin()
    monkeypatch.setenv("HERMES_LANGFUSE_CAPTURE", "full")
    assert "alice@acme.example" in mod._capture_content("alice@acme.example")
