"""Behavioral socket guard for enforced model and media entry points.

Uses only a local fake gateway and rejects any other DNS or socket connection.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.paths.append(self.path)
        data = json.dumps({
            "id": "x", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def guarded_gateway(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.paths = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    key = tmp_path / "gw.key"
    key.write_text("sk-gw-guard", encoding="utf-8")
    monkeypatch.setenv("SP_MODEL_GATEWAY_KEY_FILE", str(key))
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "FAL_KEY",
                "ELEVENLABS_API_KEY", "GROQ_API_KEY", "XAI_API_KEY", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.setenv(var, "sk-provider-guard")
    monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-guard")
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "m", "provider": "openrouter"},
        "model_gateway": {"base_url": f"http://127.0.0.1:{port}/v1", "enforce": True},
        "tts": {"provider": "elevenlabs"},
        "stt": {"enabled": True, "provider": "groq"},
        "image_gen": {"provider": "fal"},
        "video_gen": {"provider": "fal"},
    }), encoding="utf-8")
    from agent import model_gateway

    model_gateway.reset_for_tests()
    import agent.auxiliary_client as aux

    aux._reset_aux_unhealthy_cache()
    with aux._client_cache_lock:
        aux._client_cache.clear()

    connects = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            connects.append(address)
            if not (address[0] in {"127.0.0.1", "::1"} and address[1] == port):
                raise ConnectionRefusedError(f"socket guard: {address!r}")
        return real_connect(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if host not in {"127.0.0.1", "::1", "localhost", None}:
            connects.append((host, "dns"))
            raise socket.gaierror(f"socket guard: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    try:
        yield server, port, connects
    finally:
        server.shutdown()
        server.server_close()


def test_socket_guard_sees_only_the_gateway(guarded_gateway, tmp_path):
    server, port, connects = guarded_gateway
    from agent import image_gen_registry, video_gen_registry
    from agent.auxiliary_client import call_llm, resolve_provider_client
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from tools import managed_tool_gateway, transcription_tools, tts_tool

    gateway_url = f"http://127.0.0.1:{port}/v1"
    for provider_id in sorted(PROVIDER_REGISTRY):
        runtime = resolve_runtime_provider(requested=provider_id)
        assert runtime["base_url"] == gateway_url, provider_id
        client, _model = resolve_provider_client(provider_id, "m")
        assert str(client.base_url).rstrip("/") == gateway_url, provider_id
    assert json.loads(tts_tool.text_to_speech_tool("hi"))["error_code"] == "gateway_required"
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 64)
    assert transcription_tools.transcribe_audio(str(wav)).get("error_code") in {"gateway_required", None}
    for registry, section in ((image_gen_registry, "image_gen"), (video_gen_registry, "video_gen")):
        active = registry.get_active_provider()
        assert active is None or active.is_available() is False, section
    assert managed_tool_gateway.resolve_managed_tool_gateway("fal-queue") is None
    response = call_llm(task="compression", messages=[{"role": "user", "content": "x"}])
    assert response.choices[0].message.content == "ok"
    assert server.paths == ["/v1/chat/completions"]
    assert all(a[0] in {"127.0.0.1", "::1"} and a[1] == port for a in connects), connects
