"""The model gateway hook for the engine's tool backends (SynthPulse R2).

Under ``model_gateway.enforce`` the speech, transcription, image and video
tools reach only the gateway's routes; Edge TTS, ElevenLabs, fal, a
``nous`` or ``use_gateway`` selection and the Nous tool gateway get a
structured ``gateway_required`` refusal unless ``model_gateway.tool_exceptions``
names them; and a terminal child sees only the gateway's base URLs.
"""

from __future__ import annotations

import json
import socket
import stat
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from tools import tts_tool_openai

GATEWAY_KEY = "sk-gw-engine-test-0002"
PROVIDER_KEY = "sk-provider-must-not-leak"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def _reply(self, status, payload, content_type="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "size": len(raw)}
        )
        if self.path.endswith("/audio/speech"):
            self._reply(200, b"ID3" + b"\x00" * 2048, "audio/mpeg")
        elif self.path.endswith("/audio/transcriptions"):
            self._reply(200, {"text": "hello from the gateway"})
        elif self.path.endswith("/chat/completions"):
            self._reply(200, {"id": "x", "object": "chat.completion", "created": 1, "model": "m",
                              "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                           "finish_reason": "stop"}]})
        else:
            self._reply(404, {"error": {"message": "not audited"}})


@pytest.fixture
def gateway():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.port = server.server_address[1]
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def no_outbound(monkeypatch):
    """Refuse (and record) every connection that is not to loopback."""
    attempts = []
    real_connect = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(address)
            if address[0] not in {"127.0.0.1", "::1"}:
                raise ConnectionRefusedError(f"socket guard: {address!r}")
        return real_connect(sock, address)

    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host not in {"127.0.0.1", "::1", "localhost", None}:
            attempts.append((host, "dns"))
            raise socket.gaierror(f"socket guard: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts


def _write_config(config):
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    from agent import model_gateway

    model_gateway.reset_for_tests()


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "gateway.key"
    path.write_text(GATEWAY_KEY, encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("SP_MODEL_GATEWAY_KEY_FILE", str(path))
    return path


def _enforce(gateway, **sections):
    config = {"model_gateway": {"base_url": gateway.base_url, "enforce": True, "org_id": "acme"}}
    config.update(sections)
    _write_config(config)


def _assert_refusal(result, capability, backend):
    payload = json.loads(result) if isinstance(result, str) else result
    assert payload["success"] is False
    assert payload["error_code"] == "gateway_required"
    assert payload["capability"] == capability
    assert payload["backend"] == backend
    return payload


# ---------------------------------------------------------------------------
# Text to speech
# ---------------------------------------------------------------------------


def test_edge_default_gets_a_structured_refusal(gateway, key_file, no_outbound, monkeypatch):
    from tools import tts_tool

    monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: (_ for _ in ()).throw(AssertionError("edge used")))
    _enforce(gateway)
    _assert_refusal(tts_tool.text_to_speech_tool("hello"), "tts", "edge")
    _assert_refusal(tts_tool.text_to_speech_tool("hello", provider="elevenlabs"), "tts", "elevenlabs")
    assert tts_tool.check_tts_requirements() is False
    assert no_outbound == []


def test_nous_and_use_gateway_selections_are_refused(gateway, key_file):
    from agent.model_gateway import ModelGatewayRefused
    from tools import tts_tool

    _enforce(gateway, tts={"provider": "nous"})
    _assert_refusal(tts_tool.text_to_speech_tool("hello"), "tts", "nous")
    _enforce(gateway, tts={"provider": "openai", "use_gateway": True})
    with pytest.raises(ModelGatewayRefused):
        tts_tool_openai._resolve_openai_audio_client_config()


def test_openai_speech_reaches_only_the_gateway(gateway, key_file, no_outbound, monkeypatch, tmp_path):
    from tools import tts_tool

    monkeypatch.setenv("OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", PROVIDER_KEY)
    _enforce(gateway, tts={"provider": "openai"})
    assert tts_tool_openai._resolve_openai_audio_client_config() == (GATEWAY_KEY, gateway.base_url, False)
    out = tmp_path / "speech.mp3"
    tts_tool.text_to_speech_tool("hello there", output_path=str(out))
    speech = [r for r in gateway.requests if r["path"] == "/v1/audio/speech"]
    assert speech, gateway.requests
    assert speech[0]["headers"]["authorization"] == f"Bearer {GATEWAY_KEY}"
    assert all(a[0] in {"127.0.0.1", "::1"} for a in no_outbound)


def test_openai_speech_on_a_provider_url_is_refused(gateway, key_file):
    from agent.model_gateway import ModelGatewayRefused
    from tools import tts_tool

    _enforce(gateway, tts={"provider": "openai", "openai": {"base_url": "https://api.openai.com/v1"}})
    with pytest.raises(ModelGatewayRefused):
        tts_tool_openai._resolve_openai_audio_client_config()
    _assert_refusal(tts_tool.text_to_speech_tool("hello"), "tts", "openai")


def test_local_engines_and_named_exceptions_pass(gateway, key_file):
    from tools import tts_tool

    _enforce(gateway)
    assert tts_tool._get_provider({"provider": "piper"}) == "piper"
    _write_config({"model_gateway": {"base_url": gateway.base_url, "enforce": True, "tool_exceptions": ["tts:edge"]}})
    assert tts_tool._get_provider({}) == "edge"


def test_tts_unchanged_without_enforcement():
    from tools import tts_tool

    _write_config({})
    assert tts_tool._get_provider({}) == "edge"
    assert tts_tool._get_provider({"provider": "nous"}) == "openai"
    assert tts_tool._get_provider({}, override="ElevenLabs ") == "elevenlabs"


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 8000)
    return path


def test_explicit_cloud_stt_gets_a_structured_refusal(gateway, key_file, no_outbound, monkeypatch, tmp_path):
    from tools import transcription_tools as tt

    monkeypatch.setenv("GROQ_API_KEY", PROVIDER_KEY)
    _enforce(gateway, stt={"enabled": True, "provider": "groq"})
    assert tt._get_provider(tt._load_stt_config()) == "none"
    result = tt.transcribe_audio(str(_wav(tmp_path / "a.wav")))
    _assert_refusal(result, "stt", "groq")
    assert result["transcript"] == ""
    assert no_outbound == []


def test_openai_transcription_reaches_only_the_gateway(gateway, key_file, no_outbound, monkeypatch, tmp_path):
    from tools import transcription_tools as tt

    monkeypatch.setenv("OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setattr(tt, "_HAS_FASTER_WHISPER", False)
    monkeypatch.setattr(tt, "_has_local_command", lambda: False)
    _enforce(gateway, stt={"enabled": True, "provider": "openai"})
    assert tt._get_provider(tt._load_stt_config()) == "openai"
    result = tt.transcribe_audio(str(_wav(tmp_path / "b.wav")))
    assert result.get("success") is True, result
    assert "gateway" in result["transcript"]
    calls = [r for r in gateway.requests if r["path"] == "/v1/audio/transcriptions"]
    assert calls and calls[0]["headers"]["authorization"] == f"Bearer {GATEWAY_KEY}"
    assert all(a[0] in {"127.0.0.1", "::1"} for a in no_outbound)


def test_stt_prefers_local_and_refuses_nous(gateway, key_file, monkeypatch):
    from tools import transcription_tools as tt

    monkeypatch.setattr(tt, "_HAS_FASTER_WHISPER", True)
    _enforce(gateway, stt={"enabled": True, "provider": "local"})
    assert tt._get_provider(tt._load_stt_config()) == "local"
    _enforce(gateway, stt={"enabled": True, "provider": "nous"})
    assert tt._get_provider(tt._load_stt_config()) == "none"
    refusal = tt._stt_model_gateway_refusal("none", tt._load_stt_config())
    assert refusal["error_code"] == "gateway_required" and refusal["backend"] == "nous"


# ---------------------------------------------------------------------------
# Image and video registries, direct fal, the Nous tool gateway
# ---------------------------------------------------------------------------


def _fake_image_provider(name, base_url=None):
    from agent.image_gen_provider import ImageGenProvider

    class _P(ImageGenProvider):
        @property
        def name(self):
            return name

        def generate(self, prompt, aspect_ratio="landscape", **kwargs):
            raise AssertionError(f"{name} generate reached")

    provider = _P()
    if base_url is not None:
        provider.base_url = base_url
    return provider


def _fake_video_provider(name):
    from agent.video_gen_provider import VideoGenProvider

    class _V(VideoGenProvider):
        @property
        def name(self):
            return name

        def generate(self, prompt, **kwargs):
            raise AssertionError(f"{name} generate reached")

    return _V()


def test_image_provider_only_on_the_gateway(gateway, key_file, monkeypatch):
    from agent import image_gen_registry as reg

    reg._reset_for_tests()
    try:
        reg.register_provider(_fake_image_provider("openai"))
        reg.register_provider(_fake_image_provider("fal"))
        _enforce(gateway, image_gen={"provider": "openai"})
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        refused = reg.get_active_provider()
        assert refused.is_available() is False
        out = refused.generate("a cat")
        assert out["error_type"] == "gateway_required" and out["success"] is False
        monkeypatch.setenv("OPENAI_BASE_URL", gateway.base_url)
        assert reg.get_active_provider().name == "openai"
        with pytest.raises(AssertionError, match="openai generate reached"):
            reg.get_active_provider().generate("a cat")
        fal = reg.get_provider("fal")
        assert fal.generate("a cat")["error_type"] == "gateway_required"
        _write_config({})
        with pytest.raises(AssertionError, match="fal generate reached"):
            reg.get_provider("fal").generate("a cat")
    finally:
        reg._reset_for_tests()


def test_video_provider_fal_refused(gateway, key_file, monkeypatch):
    from agent import video_gen_registry as reg

    monkeypatch.setenv("FAL_KEY", PROVIDER_KEY)
    reg._reset_for_tests()
    try:
        reg.register_provider(_fake_video_provider("fal"))
        _enforce(gateway, video_gen={"provider": "fal", "use_gateway": False})
        active = reg.get_active_provider()
        assert active.is_available() is False
        out = active.generate(prompt="a wave")
        assert out["error_type"] == "gateway_required" and out["video"] is None
    finally:
        reg._reset_for_tests()


def test_direct_fal_key_is_refused(gateway, key_file, monkeypatch):
    from tools import tool_backend_helpers as tbh

    monkeypatch.setenv("FAL_KEY", PROVIDER_KEY)
    _write_config({})
    assert tbh.fal_key_is_configured() is True
    _enforce(gateway)
    assert tbh.fal_key_is_configured() is False


def test_in_tree_fal_image_path_is_refused(gateway, key_file, no_outbound, monkeypatch):
    from tools import image_generation_tool as it

    monkeypatch.setenv("FAL_KEY", PROVIDER_KEY)
    monkeypatch.setattr(it, "_submit_fal_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fal")))
    _enforce(gateway)
    result = json.loads(it.image_generate_tool(prompt="a cat"))
    assert result["success"] is False
    assert no_outbound == []


def test_use_gateway_selection_raises_the_refusal(gateway, key_file):
    from agent.model_gateway import ModelGatewayRefused
    from tools import tool_backend_helpers as tbh

    _enforce(gateway, image_gen={"provider": "fal", "use_gateway": True})
    with pytest.raises(ModelGatewayRefused) as excinfo:
        tbh.read_selection("image_gen")
    assert excinfo.value.backend == "nous"
    assert tbh.selection_exists("image_gen") is True
    assert tbh.prefers_gateway("image_gen") is False
    _enforce(gateway, web={"backend": "firecrawl", "use_gateway": True})
    assert tbh.read_selection("web") == "nous"  # not model traffic


def test_nous_tool_gateway_refused_unless_named(gateway, key_file, monkeypatch):
    from agent.model_gateway import ModelGatewayRefused
    from tools import managed_tool_gateway as mtg
    from tools import tool_backend_helpers as tbh

    monkeypatch.setattr(mtg, "managed_nous_tools_enabled", lambda **k: True)
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-user-token")
    _write_config({})
    assert mtg.resolve_managed_tool_gateway("openai-audio") is not None
    _enforce(gateway)
    assert mtg.resolve_managed_tool_gateway("openai-audio") is None
    assert mtg.resolve_managed_tool_gateway("firecrawl") is None
    with pytest.raises(ModelGatewayRefused):
        mtg.build_vendor_gateway_url("fal-queue")
    assert mtg.resolve_managed_tool_gateway("krea") is None
    assert tbh.managed_nous_tools_enabled(capability="tts") is False
    _write_config({"model_gateway": {"base_url": gateway.base_url, "enforce": True,
                                     "tool_exceptions": ["nous:openai-audio"]}})
    assert mtg.resolve_managed_tool_gateway("openai-audio") is not None
    assert mtg.resolve_managed_tool_gateway("fal-queue") is None


# ---------------------------------------------------------------------------
# Terminal children
# ---------------------------------------------------------------------------


def _write_fake_cli(bin_dir: Path, name: str, script: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{script}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


_FAKE_CODEX = r'''
import json, os, urllib.request
base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
    data=json.dumps({"model": "codex", "messages": []}).encode(),
    headers={"Authorization": "Bearer " + os.environ.get("OPENAI_API_KEY", ""), "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=10).status)
'''

_FAKE_CLAUDE = r'''
import json, os, urllib.error, urllib.request
base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
req = urllib.request.Request(base.rstrip("/") + "/v1/messages",
    data=json.dumps({"model": "claude", "messages": []}).encode(),
    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(req, timeout=10).status)
except urllib.error.HTTPError as exc:
    print(exc.code)
'''


def test_terminal_child_sees_only_the_gateway(gateway, key_file, monkeypatch, tmp_path):
    from tools import terminal_tool_backends as terminal_tool

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_cli(bin_dir, "codex", _FAKE_CODEX)
    _write_fake_cli(bin_dir, "claude", _FAKE_CLAUDE)
    monkeypatch.setenv("OPENROUTER_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", PROVIDER_KEY)
    monkeypatch.setenv("HERMES_DWD_IDENTITY", "alice@acme.example")
    _enforce(gateway)
    env = terminal_tool._create_environment("local", "", str(tmp_path), 30, task_id="r2-terminal")
    try:
        result = env.execute("env", timeout=30)
        lines = dict(
            line.split("=", 1) for line in result["output"].splitlines() if "=" in line
        )
        assert lines["OPENAI_BASE_URL"] == gateway.base_url
        assert lines["OPENAI_API_KEY"] == GATEWAY_KEY
        assert lines["ANTHROPIC_BASE_URL"] == gateway.base_url[: -len("/v1")]
        assert lines["ANTHROPIC_AUTH_TOKEN"] == GATEWAY_KEY
        assert PROVIDER_KEY not in result["output"]
        # Untrusted ambient DWD identity remains stripped by upstream governance.
        assert not lines.get("HERMES_DWD_IDENTITY")
        # Absolute paths: the fake CLIs, never a real provider CLI on PATH.
        assert "200" in env.execute(f"{bin_dir}/codex", timeout=30)["output"]
        assert "404" in env.execute(f"{bin_dir}/claude", timeout=30)["output"]
    finally:
        env.cleanup()
    paths = [r["path"] for r in gateway.requests]
    assert "/v1/chat/completions" in paths and "/v1/messages" in paths
    for request in gateway.requests:
        assert request["headers"]["authorization"] == f"Bearer {GATEWAY_KEY}"


def test_terminal_child_unchanged_without_enforcement(tmp_path):
    from tools import terminal_tool_backends as terminal_tool

    _write_config({})
    env = terminal_tool._create_environment("local", "", str(tmp_path), 30, task_id="r2-terminal-off")
    try:
        assert env.env == {}
    finally:
        env.cleanup()


@pytest.mark.parametrize("module,capability", [("image_gen_registry", "image_gen"), ("video_gen_registry", "video_gen")])
def test_direct_registry_availability_is_not_probed_under_enforcement(gateway, key_file, monkeypatch, module, capability):
    from agent import image_gen_registry, video_gen_registry

    registry = {"image_gen_registry": image_gen_registry, "video_gen_registry": video_gen_registry}[module]
    class DirectProvider:
        name = "fal"
        def is_available(self):
            pytest.fail("a refused provider must never perform an availability probe")
    _enforce(gateway)
    monkeypatch.setattr(registry._registry, "merged", lambda: {"fal": DirectProvider()})
    assert registry.get_active_provider() is None
