"""Model gateway enforcement for the engine's model clients (SynthPulse R2).

Covers ``agent/model_gateway.py`` and the model-client entry points that
consult it: ``hermes_cli.runtime_provider.resolve_runtime_provider``, the
auxiliary client (``resolve_provider_client``, the step 3 chain, the Codex,
Anthropic and Gemini native paths, the payment fallback), the main agent's
client keyword arguments (``run_agent``) and every completion path
(``agent.chat_completion_helpers``).

Every network test talks to an in-process fake gateway on 127.0.0.1; a
socket guard records every outbound connection attempt.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import yaml

SALT = "sp-test-salt-v1"
PERSON = "Alice.Example@Acme.Example "
PSEUDONYM = "u_00ce9e9f4c1f"  # STACK modules/omniroute/key_registry.py test vector
GATEWAY_KEY = "sk-gw-engine-test-0001"
PROVIDER_KEY = "sk-or-provider-must-not-leak"


# ---------------------------------------------------------------------------
# Fake gateway and socket guard
# ---------------------------------------------------------------------------


class _GatewayHandler(BaseHTTPRequestHandler):
    server_version = "FakeGateway/1"

    def log_message(self, *args, **kwargs):  # noqa: D401 - silence
        pass

    def _record(self, body):
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )

    def _send_json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._record(None)
        if self.path.rstrip("/").endswith("/models"):
            self._send_json(200, {"object": "list", "data": [{"id": "gw-model", "object": "model"}]})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            body = {"_raw_len": len(raw)}
        self._record(body)
        status = self.server.status
        if status != 200:
            self._send_json(status, {"error": {"message": "gateway says no", "type": "insufficient_quota"}})
            return
        if self.path.endswith("/chat/completions"):
            text = "gateway reply"
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in (
                    {"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"),
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                    {"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"),
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
                ):
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            self._send_json(200, {
                "id": "c1", "object": "chat.completion", "created": 1, "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            })
            return
        self._send_json(404, {"error": {"message": "route not audited"}})


class FakeGateway:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.requests = []
        self.server.status = 200
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def requests(self):
        return self.server.requests

    def set_status(self, status):
        self.server.status = status

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_gateway():
    gw = FakeGateway()
    try:
        yield gw
    finally:
        gw.stop()


class SocketGuard:
    """Records every outbound connect and name lookup; refuses non-loopback."""

    def __init__(self):
        self.connects = []
        self.lookups = []

    def install(self, monkeypatch):
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        real_getaddrinfo = socket.getaddrinfo
        guard = self

        def _check(address):
            host = address[0] if isinstance(address, tuple) else str(address)
            guard.connects.append(address)
            if isinstance(address, tuple) and host not in {"127.0.0.1", "::1", "localhost"}:
                raise ConnectionRefusedError(f"socket guard: outbound connection to {address!r}")

        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                _check(address)
            return real_connect(sock, address)

        def connect_ex(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                _check(address)
            return real_connect_ex(sock, address)

        def getaddrinfo(host, *args, **kwargs):
            guard.lookups.append(host)
            if host not in {"127.0.0.1", "::1", "localhost", None}:
                raise socket.gaierror(f"socket guard: lookup of {host!r}")
            return real_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        return self

    def outbound(self, allowed_port):
        return [
            a for a in self.connects
            if not (isinstance(a, tuple) and a[0] in {"127.0.0.1", "::1"} and a[1] == allowed_port)
        ]


@pytest.fixture
def socket_guard(monkeypatch):
    return SocketGuard().install(monkeypatch)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _hermes_home():
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _write_config(config):
    home = _hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _reset_engine_caches():
    from agent import model_gateway

    model_gateway.reset_for_tests()
    try:
        import agent.auxiliary_client as aux

        aux._reset_aux_unhealthy_cache()
        with aux._client_cache_lock:
            aux._client_cache.clear()
    except Exception:
        pass


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    key_file = tmp_path / "gateway.key"
    key_file.write_text(GATEWAY_KEY + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    salt_file = tmp_path / "org.salt"
    salt_file.write_text(SALT, encoding="utf-8")
    salt_file.chmod(0o600)
    monkeypatch.setenv("SP_MODEL_GATEWAY_KEY_FILE", str(key_file))
    monkeypatch.setenv("SP_AUDIT_ORG_SALT_FILE", str(salt_file))
    return SimpleNamespace(key_file=key_file, salt_file=salt_file)


def _gateway_config(base_url, *, enforce=True, provider="openrouter", **extra_block):
    block = {
        "base_url": base_url,
        "enforce": enforce,
        "org_id": "acme",
        "installation_id": "acme/prod",
        "module": "engine",
        "principal": PERSON,
    }
    block.update(extra_block)
    return {
        "model": {"default": "gw-model", "provider": provider, "context_length": 128000},
        "model_gateway": block,
    }


@pytest.fixture
def enforced(fake_gateway, secrets_dir, monkeypatch):
    """Enforcement on, a provider key present in the environment, a fake gateway."""
    monkeypatch.setenv("OPENROUTER_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-r2-test")
    _write_config(_gateway_config(fake_gateway.base_url))
    _reset_engine_caches()
    yield fake_gateway
    _reset_engine_caches()


# ---------------------------------------------------------------------------
# model_gateway: settings, identity, keys
# ---------------------------------------------------------------------------


def test_defaults_change_nothing():
    _write_config({})
    _reset_engine_caches()
    from agent import model_gateway

    s = model_gateway.settings()
    assert s.enforce is False and s.base_url == ""
    assert model_gateway.enforced() is False
    kwargs = {"model": "m", "messages": []}
    assert model_gateway.apply_request_identity(dict(kwargs), base_url="https://openrouter.ai/api/v1") == kwargs
    assert model_gateway.cache_token() == ()
    assert model_gateway.terminal_child_env() == {}
    assert model_gateway.tool_backend_allowed("tts", "edge") is True


def test_pseudonym_matches_the_key_registry_vector():
    from agent import model_gateway

    assert model_gateway.pseudonym(PERSON, SALT) == PSEUDONYM
    assert model_gateway.pseudonym("alice.example@acme.example", SALT.encode()) == PSEUDONYM
    assert model_gateway.pseudonym(PERSON, "another-salt") != PSEUDONYM
    with pytest.raises(ValueError):
        model_gateway.pseudonym("not-an-email", SALT)
    with pytest.raises(ValueError):
        model_gateway.pseudonym(PERSON, "")


def test_env_can_switch_enforcement_on_but_never_off(monkeypatch):
    from agent import model_gateway

    _write_config({"model_gateway": {"base_url": "http://gw.internal/v1", "enforce": False}})
    monkeypatch.setenv("SP_MODEL_GATEWAY_ENFORCE", "1")
    assert model_gateway.enforced() is True
    _write_config({"model_gateway": {"base_url": "http://gw.internal/v1", "enforce": True}})
    monkeypatch.setenv("SP_MODEL_GATEWAY_ENFORCE", "0")
    assert model_gateway.enforced() is True


def test_env_url_fills_an_empty_base_url(monkeypatch):
    from agent import model_gateway

    _write_config({"model_gateway": {"enforce": True}})
    monkeypatch.setenv("SP_MODEL_GATEWAY_URL", "http://route-filter:8080/v1/")
    assert model_gateway.gateway_base_url() == "http://route-filter:8080/v1"
    assert model_gateway.anthropic_base_url() == "http://route-filter:8080"


def test_gateway_url_is_the_gateway_origin_only():
    from agent import model_gateway

    _write_config({"model_gateway": {"base_url": "http://127.0.0.1:20128/v1"}})
    assert model_gateway.is_gateway_url("http://127.0.0.1:20128/v1")
    assert model_gateway.is_gateway_url("http://127.0.0.1:20128")
    assert model_gateway.is_gateway_url("http://127.0.0.1:20128/v1/audio/speech")
    assert not model_gateway.is_gateway_url("http://127.0.0.1:8787/v1")
    assert not model_gateway.is_gateway_url("https://127.0.0.1:20128/v1")
    assert not model_gateway.is_gateway_url("http://127.0.0.1.evil.test:20128/v1")
    assert not model_gateway.is_gateway_url("https://openrouter.ai/api/v1")
    assert not model_gateway.is_gateway_url("")
    _write_config({"model_gateway": {"base_url": "https://ai.acme.example/v1"}})
    assert model_gateway.is_gateway_url("https://ai.acme.example:443/v1/messages")


def test_gateway_key_order_and_never_a_provider_key(tmp_path, monkeypatch):
    from agent import model_gateway

    monkeypatch.setenv("OPENROUTER_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("PROFILE_GATEWAY_KEY", "sk-gw-profile-env")
    cfg_file = tmp_path / "cfg.key"
    cfg_file.write_text("sk-gw-config-file", encoding="utf-8")
    env_file = tmp_path / "env.key"
    env_file.write_text("sk-gw-env-file", encoding="utf-8")
    config = {
        "model_gateway": {"base_url": "http://127.0.0.1:20128/v1", "enforce": True},
        "custom_providers": [
            {"name": "OmniRoute", "base_url": "http://127.0.0.1:20128/v1", "key_env": "PROFILE_GATEWAY_KEY"},
            {"name": "Other", "base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"},
        ],
    }
    _write_config(config)
    _reset_engine_caches()
    assert model_gateway.gateway_api_key() == "sk-gw-profile-env"
    config["model_gateway"]["key_env"] = "PROFILE_GATEWAY_KEY"
    config["model_gateway"]["key_file"] = str(cfg_file)
    _write_config(config)
    assert model_gateway.gateway_api_key() == "sk-gw-config-file"
    monkeypatch.setenv("SP_MODEL_GATEWAY_KEY_FILE", str(env_file))
    assert model_gateway.gateway_api_key() == "sk-gw-env-file"
    _write_config({"model_gateway": {"base_url": "http://127.0.0.1:20128/v1", "enforce": True}})
    monkeypatch.delenv("SP_MODEL_GATEWAY_KEY_FILE")
    assert model_gateway.gateway_api_key() == ""


def test_tool_exceptions_are_ignored_on_a_managed_installation(monkeypatch):
    from agent import model_gateway

    _write_config({"model_gateway": {"base_url": "http://gw/v1", "enforce": True, "tool_exceptions": ["tts:edge"]}})
    assert model_gateway.tool_backend_allowed("tts", "edge") is True
    monkeypatch.setenv("SP_MANAGED", "1")
    assert model_gateway.settings().tool_exceptions == ()
    assert model_gateway.tool_backend_allowed("tts", "edge") is False


def test_request_user_prefers_the_bound_person_and_never_sends_an_email(secrets_dir):
    from agent import model_gateway
    from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, governance_context
    from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject

    _write_config({"model_gateway": {"base_url": "http://gw/v1", "principal": "owner@acme.example"}})
    assert model_gateway.request_user() == model_gateway.pseudonym("owner@acme.example", SALT)
    subject = GovernanceSubject(email=PERSON)
    ctx = DashboardGovernanceContext(subject=subject, access=EffectiveAccess(subject=subject, mode="enforce"))
    with governance_context(ctx):
        assert model_gateway.request_user() == PSEUDONYM
    _write_config({"model_gateway": {"base_url": "http://gw/v1", "principal": PERSON, "user_field": "none"}})
    assert model_gateway.request_user() is None


def test_no_salt_means_no_user_field(monkeypatch):
    from agent import model_gateway

    monkeypatch.delenv("SP_AUDIT_ORG_SALT_FILE", raising=False)
    _write_config({"model_gateway": {"base_url": "http://gw/v1", "principal": PERSON}})
    _reset_engine_caches()
    kwargs = model_gateway.apply_request_identity({"model": "m"}, base_url="http://gw/v1", session_id="s1")
    assert "user" not in kwargs
    assert PERSON.strip() not in json.dumps(kwargs)
    assert kwargs["extra_headers"] == {"X-SP-Session": "s1"}


def test_identity_per_wire(secrets_dir):
    from agent import model_gateway

    _write_config({"model_gateway": {"base_url": "http://gw/v1", "principal": PERSON}})
    chat = model_gateway.apply_request_identity({"model": "m"}, base_url="http://gw/v1", session_id="s\r\nX-Evil: 1")
    assert chat["user"] == PSEUDONYM
    assert chat["extra_headers"] == {"X-SP-Session": "sX-Evil:1"}
    responses = model_gateway.apply_request_identity(
        {"model": "m", "extra_body": {"a": 1}}, base_url="http://gw/v1", api_mode="codex_responses", session_id="s1",
    )
    assert responses["extra_body"] == {"a": 1, "user": PSEUDONYM} and "user" not in responses
    messages = model_gateway.apply_request_identity(
        {"model": "m", "extra_headers": {"anthropic-beta": "x"}}, base_url="http://gw", api_mode="anthropic_messages",
        session_id="s1",
    )
    assert messages["metadata"] == {"user_id": PSEUDONYM}
    assert messages["extra_headers"] == {"anthropic-beta": "x", "X-SP-Session": "s1"}
    other = model_gateway.apply_request_identity({"model": "m"}, base_url="https://api.openai.com/v1", session_id="s1")
    assert other == {"model": "m"}


# ---------------------------------------------------------------------------
# runtime_provider.resolve_runtime_provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("requested", ["openrouter", "anthropic", "openai-codex", "nous", "gemini", "auto", "custom"])
def test_every_runtime_resolves_to_the_gateway(enforced, requested):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested=requested)
    assert runtime["base_url"] == enforced.base_url
    assert runtime["api_key"] == GATEWAY_KEY
    assert runtime["api_key"] != PROVIDER_KEY
    assert runtime["provider"] == "custom"
    assert runtime["api_mode"] == "chat_completions"
    assert runtime["source"] == "model-gateway"


def test_runtime_ignores_an_explicit_provider_endpoint_and_key(enforced):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(
        requested="custom", explicit_base_url="https://api.deepseek.com/v1", explicit_api_key=PROVIDER_KEY,
    )
    assert runtime["base_url"] == enforced.base_url
    assert runtime["api_key"] == GATEWAY_KEY


def test_runtime_keeps_the_named_gateway_provider(fake_gateway, monkeypatch):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.delenv("SP_MODEL_GATEWAY_KEY_FILE", raising=False)
    monkeypatch.setenv("OMNIROUTE_PROFILE_KEY", "sk-gw-profile-yaser")
    config = _gateway_config(fake_gateway.base_url, provider="custom:omniroute")
    config["custom_providers"] = [
        {"name": "OmniRoute", "base_url": fake_gateway.base_url, "key_env": "OMNIROUTE_PROFILE_KEY"},
    ]
    _write_config(config)
    _reset_engine_caches()
    runtime = resolve_runtime_provider(requested="custom:omniroute")
    assert runtime["api_key"] == "sk-gw-profile-yaser"
    assert runtime["base_url"] == fake_gateway.base_url
    assert runtime["requested_provider"] == "custom:omniroute"


def test_runtime_fails_closed_without_url_or_key(monkeypatch):
    from agent.model_gateway import ModelGatewayUnavailable
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("OPENROUTER_API_KEY", PROVIDER_KEY)
    _write_config({"model": {"default": "m", "provider": "openrouter"}, "model_gateway": {"enforce": True}})
    _reset_engine_caches()
    with pytest.raises(ModelGatewayUnavailable):
        resolve_runtime_provider(requested="openrouter")
    _write_config({"model": {"default": "m", "provider": "openrouter"},
                   "model_gateway": {"enforce": True, "base_url": "http://127.0.0.1:9/v1"}})
    with pytest.raises(ModelGatewayUnavailable):
        resolve_runtime_provider(requested="openrouter")


def test_runtime_unchanged_without_enforcement(monkeypatch):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    monkeypatch.setenv("OPENROUTER_API_KEY", PROVIDER_KEY)
    _write_config({"model": {"default": "m", "provider": "openrouter"},
                   "model_gateway": {"base_url": "http://127.0.0.1:20128/v1", "enforce": False}})
    _reset_engine_caches()
    runtime = resolve_runtime_provider(requested="openrouter")
    assert "openrouter.ai" in runtime["base_url"]
    assert runtime.get("source") != "model-gateway"


# ---------------------------------------------------------------------------
# Auxiliary client
# ---------------------------------------------------------------------------


def _aux_tasks():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    return sorted(k for k, v in DEFAULT_CONFIG["auxiliary"].items() if isinstance(v, dict))


def test_every_auxiliary_task_reaches_the_gateway_with_the_user_field(enforced, socket_guard):
    from agent.auxiliary_client import call_llm

    tasks = _aux_tasks()
    assert len(tasks) >= 10
    for task in tasks:
        response = call_llm(task=task, messages=[{"role": "user", "content": f"task {task}"}], max_tokens=16)
        assert response.choices[0].message.content == "gateway reply", task
    posts = [r for r in enforced.requests if r["method"] == "POST"]
    assert len(posts) >= len(tasks)
    for request in posts:
        assert request["path"] == "/v1/chat/completions"
        assert request["headers"]["authorization"] == f"Bearer {GATEWAY_KEY}"
        assert request["body"]["user"] == PSEUDONYM
        assert request["headers"]["x-sp-session"] == "sess-r2-test"
    assert socket_guard.outbound(enforced.port) == []


def test_provider_chain_is_the_gateway_alone(enforced):
    import agent.auxiliary_client as aux

    chain = aux._get_provider_chain()
    assert [label for label, _ in chain] == ["gateway"]
    for fn, args in (
        (aux._try_openrouter, ()),
        (aux._try_nous, ()),
        (aux._try_custom_endpoint, ()),
        (aux._resolve_api_key_provider, ()),
        (aux._try_anthropic, ()),
        (aux._build_codex_client, ("gpt-5.4",)),
        (aux._build_xai_oauth_aux_client, ("grok-4",)),
    ):
        assert fn(*args) == (None, None), fn.__name__
    assert aux._try_azure_foundry(model="gpt-4o") == (None, None)


@pytest.mark.parametrize(
    "provider,model",
    [("openrouter", "google/gemini-3-flash"), ("openai-codex", "gpt-5.4"), ("anthropic", "claude-x"),
     ("gemini", "gemini-3-pro"), ("nous", "hermes-5"), ("auto", None), ("custom", None)],
)
def test_resolve_provider_client_returns_a_gateway_client(enforced, provider, model):
    import agent.auxiliary_client as aux

    client, resolved = aux.resolve_provider_client(
        provider, model, explicit_base_url="https://api.x.ai/v1" if provider == "custom" else None,
        explicit_api_key=PROVIDER_KEY if provider == "custom" else None,
    )
    assert not isinstance(client, (aux.CodexAuxiliaryClient, aux.AnthropicAuxiliaryClient))
    assert str(client.base_url).rstrip("/") == enforced.base_url
    assert client.api_key == GATEWAY_KEY
    assert resolved == (model or "gw-model")


def test_vision_resolves_to_the_gateway(enforced):
    import agent.auxiliary_client as aux

    provider, client, model = aux.resolve_vision_provider_client()
    assert provider == "gateway"
    assert str(client.base_url).rstrip("/") == enforced.base_url
    assert model == "gw-model"


def test_unhealthy_gateway_raises_instead_of_choosing_openrouter(enforced, monkeypatch):
    import agent.auxiliary_client as aux
    from agent.model_gateway import ModelGatewayUnavailable

    def _never(*a, **k):
        raise AssertionError("OpenRouter lane touched under enforcement")

    monkeypatch.setattr(aux, "_try_openrouter", _never)
    aux._mark_provider_unhealthy("openrouter")  # any label means the gateway under enforcement
    assert aux._is_provider_unhealthy("gateway")
    with pytest.raises(ModelGatewayUnavailable):
        aux.call_llm(task="compression", messages=[{"role": "user", "content": "x"}])
    with pytest.raises(ModelGatewayUnavailable):
        aux.resolve_provider_client("auto")


def test_payment_error_on_the_gateway_never_falls_back(enforced, socket_guard, monkeypatch):
    import agent.auxiliary_client as aux
    from agent.model_gateway import ModelGatewayUnavailable

    monkeypatch.setattr(aux, "_try_openrouter", lambda *a, **k: (_ for _ in ()).throw(AssertionError("openrouter")))
    enforced.set_status(402)
    with pytest.raises(Exception) as first:
        aux.call_llm(task="title_generation", messages=[{"role": "user", "content": "x"}])
    assert "openrouter" not in str(first.value).lower()
    assert aux._try_payment_fallback("auto", "title_generation") == (None, None, "")
    with pytest.raises(ModelGatewayUnavailable):
        aux.call_llm(task="title_generation", messages=[{"role": "user", "content": "x"}])
    assert socket_guard.outbound(enforced.port) == []


def test_cache_key_shape_is_unchanged_without_enforcement():
    import agent.auxiliary_client as aux

    _write_config({"model_gateway": {"base_url": "http://gw/v1", "enforce": False}})
    _reset_engine_caches()
    plain = aux._client_cache_key("openrouter", async_mode=False, model="m")
    _write_config({"model_gateway": {"base_url": "http://gw/v1", "enforce": True}})
    enforced_key = aux._client_cache_key("openrouter", async_mode=False, model="m")
    assert enforced_key[:len(plain)] == plain and enforced_key[len(plain):len(plain)+2] == ("gateway", "http://gw/v1")


# ---------------------------------------------------------------------------
# Main agent: client keyword arguments and completion paths
# ---------------------------------------------------------------------------


def _agent(base_url, api_key=GATEWAY_KEY, provider="custom", **kwargs):
    from run_agent import AIAgent

    return AIAgent(
        api_key=api_key, base_url=base_url, provider=provider, model="gw-model",
        max_iterations=2, enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
        skip_memory=True, save_trajectories=False, platform="cli", **kwargs,
    )


def test_agent_client_for_a_direct_endpoint_is_refused(enforced):
    from agent.model_gateway import ModelGatewayRefused

    agent = _agent(enforced.base_url)
    with pytest.raises(ModelGatewayRefused) as excinfo:
        agent._model_gateway_client_kwargs({"api_key": PROVIDER_KEY, "base_url": "https://openrouter.ai/api/v1"})
    assert excinfo.value.code == "gateway_required"
    # Agent construction on a direct endpoint fails with the same refusal.
    with pytest.raises(Exception, match="gateway_required"):
        _agent("https://openrouter.ai/api/v1", api_key=PROVIDER_KEY, provider="openrouter")


def test_agent_client_on_the_gateway_carries_the_session_header(enforced):
    agent = _agent(enforced.base_url)
    kwargs = agent._model_gateway_client_kwargs({"api_key": GATEWAY_KEY, "base_url": enforced.base_url})
    assert kwargs["default_headers"]["X-SP-Session"] == agent.session_id


def test_every_completion_path_carries_the_user_field(enforced):
    from agent.chat_completion_helpers import build_api_kwargs

    agent = _agent(enforced.base_url)
    kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
    assert kwargs["user"] == PSEUDONYM
    assert kwargs["extra_headers"]["X-SP-Session"] == agent.session_id


def test_bedrock_and_direct_endpoints_are_refused_per_request(enforced):
    from agent.chat_completion_helpers import build_api_kwargs
    from agent.model_gateway import ModelGatewayRefused

    agent = _agent(enforced.base_url)
    agent.base_url = "https://api.anthropic.com"
    with pytest.raises(ModelGatewayRefused):
        build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
    agent.base_url = enforced.base_url
    agent.api_mode = "bedrock_converse"
    with pytest.raises(ModelGatewayRefused):
        build_api_kwargs(agent, [{"role": "user", "content": "hi"}])


def test_completion_kwargs_unchanged_without_enforcement(fake_gateway, secrets_dir):
    from agent.chat_completion_helpers import build_api_kwargs

    _write_config(_gateway_config("http://127.0.0.1:1/v1", enforce=False))
    _reset_engine_caches()
    agent = _agent(fake_gateway.base_url, api_key="sk-local")
    kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
    assert "user" not in kwargs
    assert "X-SP-Session" not in (kwargs.get("extra_headers") or {})


def test_fallback_activation_stays_on_the_gateway(enforced):
    agent = _agent(
        enforced.base_url,
        fallback_model=[{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
                        {"provider": "anthropic", "model": "claude-opus-4"}],
    )
    assert agent._try_activate_fallback() is True
    assert agent.base_url.rstrip("/") == enforced.base_url
    assert agent.provider == "custom"
    assert agent.api_mode == "chat_completions"
    assert agent.api_key == GATEWAY_KEY


# ---------------------------------------------------------------------------
# Acceptance: a turn reaches the gateway; with the gateway down it fails
# ---------------------------------------------------------------------------


def test_a_turn_reaches_the_gateway_with_identity(enforced, socket_guard):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested="openrouter")
    agent = _agent(runtime["base_url"], api_key=runtime["api_key"], provider=runtime["provider"],
                   api_mode=runtime["api_mode"])
    result = agent.run_conversation("hello", conversation_history=[], task_id="r2-turn")
    assert "gateway reply" in (result.get("final_response") or "")
    completions = [r for r in enforced.requests if r["path"] == "/v1/chat/completions"]
    assert completions, enforced.requests
    for request in completions:
        assert request["body"]["user"] == PSEUDONYM
        assert request["headers"]["authorization"] == f"Bearer {GATEWAY_KEY}"
        assert request["headers"].get("x-sp-session")
    # Metadata probes also went to the gateway, and nothing carried a provider key.
    assert all(PROVIDER_KEY not in json.dumps(r) for r in enforced.requests)
    assert socket_guard.outbound(enforced.port) == []


def test_with_the_gateway_down_the_turn_fails_and_nothing_else_is_called(enforced, socket_guard, monkeypatch):
    import agent.retry_utils as loop

    monkeypatch.setattr(loop, "jittered_backoff", lambda *a, **k: 0.01)
    agent = _agent(
        enforced.base_url,
        fallback_model=[{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}],
    )
    agent._api_max_retries = 1
    port = enforced.port
    enforced.stop()
    result = agent.run_conversation("hello", conversation_history=[], task_id="r2-down")
    final = (result.get("final_response") or "")
    assert "gateway reply" not in final
    assert result.get("failed") or result.get("error") or not final
    assert socket_guard.outbound(port) == []
    assert all(host in {"127.0.0.1", "::1", "localhost", None} for host in socket_guard.lookups)


def test_routed_profile_never_borrows_installation_key(tmp_path, monkeypatch):
    from agent import model_gateway, secret_scope

    root_key = tmp_path / "installation.key"
    root_key.write_text("installation-secret")
    profile_key = tmp_path / "profile.key"
    profile_key.write_text("own-profile-secret")
    monkeypatch.setenv("SP_MODEL_GATEWAY_KEY_FILE", str(root_key))
    monkeypatch.setenv("OWN_GATEWAY_KEY", "process-secret")
    config = {"model_gateway": {"base_url": "http://gateway.test/v1", "enforce": True,
                                 "key_env": "OWN_GATEWAY_KEY"}}
    mode = secret_scope.set_multiplex_context(True)
    scope = secret_scope.set_secret_scope({})
    try:
        assert model_gateway.gateway_api_key(config=config, fallback="installation-fallback") == ""
        with pytest.raises(model_gateway.ModelGatewayUnavailable):
            model_gateway.require_gateway(config=config, fallback_key="installation-fallback")
        own = secret_scope.set_secret_scope({"SP_MODEL_GATEWAY_KEY_FILE": str(profile_key)})
        try:
            assert model_gateway.gateway_api_key(config=config) == "own-profile-secret"
        finally:
            secret_scope.reset_secret_scope(own)
        own = secret_scope.set_secret_scope({"OWN_GATEWAY_KEY": "own-env-secret"})
        try:
            assert model_gateway.gateway_api_key(config=config) == "own-env-secret"
        finally:
            secret_scope.reset_secret_scope(own)
    finally:
        secret_scope.reset_secret_scope(scope)
        secret_scope.reset_multiplex_context(mode)


def test_terminal_cannot_launch_when_gateway_key_is_missing(monkeypatch):
    from agent import model_gateway
    from tools import terminal_tool_backends

    def missing(**kwargs):
        raise model_gateway.ModelGatewayUnavailable("gateway key missing")
    monkeypatch.setattr(model_gateway, "terminal_child_env", missing)
    with pytest.raises(model_gateway.ModelGatewayUnavailable):
        terminal_tool_backends._model_gateway_child_env()


def test_auxiliary_cache_follows_scoped_gateway_key(monkeypatch):
    from agent import model_gateway, secret_scope
    import agent.auxiliary_client as aux

    _write_config({"model_gateway": {"base_url": "http://gateway.test/v1", "enforce": True,
                                     "key_env": "OWN_GATEWAY_KEY"}})
    mode = secret_scope.set_multiplex_context(True)
    scope = secret_scope.set_secret_scope({"OWN_GATEWAY_KEY": "profile-one"})
    try:
        first, _ = aux._get_cached_client("auto", "model", False)
        assert first.api_key == "profile-one"
        second_scope = secret_scope.set_secret_scope({"OWN_GATEWAY_KEY": "profile-two"})
        try:
            second, _ = aux._get_cached_client("auto", "model", False)
            assert second is not first
            assert second.api_key == "profile-two"
        finally:
            secret_scope.reset_secret_scope(second_scope)
        empty = secret_scope.set_secret_scope({})
        try:
            with pytest.raises(model_gateway.ModelGatewayUnavailable):
                aux._get_cached_client("auto", "model", False)
        finally:
            secret_scope.reset_secret_scope(empty)
    finally:
        aux.shutdown_cached_clients()
        secret_scope.reset_secret_scope(scope)
        secret_scope.reset_multiplex_context(mode)


@pytest.mark.asyncio
async def test_async_auxiliary_request_uses_gateway_identity(enforced, socket_guard):
    import agent.auxiliary_client as aux

    response = await aux.async_call_llm(task="compression", messages=[{"role": "user", "content": "hello"}])
    assert response.choices[0].message.content == "gateway reply"
    requests = [item for item in enforced.requests if item["path"] == "/v1/chat/completions"]
    assert requests
    assert requests[-1]["body"]["user"] == PSEUDONYM
    assert requests[-1]["headers"]["authorization"] == f"Bearer {GATEWAY_KEY}"
    assert socket_guard.outbound(enforced.port) == []
    aux.shutdown_cached_clients()
