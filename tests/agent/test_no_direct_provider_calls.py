"""No direct provider path at the engine's model entry points (SynthPulse R2).

A scan of the whole multi-provider engine can never pass: it is full of
provider adapters. This test scans exactly the model-client and tool-backend
entry points that R2 owns, and proves each one consults ``agent.model_gateway``
before it can build a client or pick a backend. ``agent/model_gateway.py``
itself names no provider host, builds no provider client and reads no
provider key. A socket guard then drives every entry point under enforcement
and proves no outbound connection other than the gateway's.

The provider host list is a copy of STACK ``modules/egress/provider-hosts.txt``
(E.7.1, sha256 01db555af6b7590c208d7de6200ac3a86356f1960efb0daf12400838fdb7c573);
changes to it are orchestrator requests.
"""

from __future__ import annotations

import ast
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

PROVIDER_HOSTS = (
    "api.openai.com", "auth.openai.com", "chatgpt.com", "*.openai.azure.com",
    "*.services.ai.azure.com", "*.models.ai.azure.com", "*.inference.ai.azure.com",
    "*.cognitiveservices.azure.com", "*.tts.speech.microsoft.com", "*.stt.speech.microsoft.com",
    "speech.platform.bing.com", "api.anthropic.com", "generativelanguage.googleapis.com",
    "cloudcode-pa.googleapis.com", "aiplatform.googleapis.com",
    "europe-west1-aiplatform.googleapis.com", "europe-west3-aiplatform.googleapis.com",
    "europe-west4-aiplatform.googleapis.com", "europe-west8-aiplatform.googleapis.com",
    "europe-west9-aiplatform.googleapis.com", "europe-north1-aiplatform.googleapis.com",
    "europe-central2-aiplatform.googleapis.com", "europe-southwest1-aiplatform.googleapis.com",
    "us-central1-aiplatform.googleapis.com", "us-east5-aiplatform.googleapis.com",
    "texttospeech.googleapis.com", "eu-texttospeech.googleapis.com", "speech.googleapis.com",
    "eu-speech.googleapis.com", "bedrock-runtime.eu-central-1.amazonaws.com",
    "bedrock-runtime.eu-west-1.amazonaws.com", "bedrock-runtime.eu-west-3.amazonaws.com",
    "bedrock-runtime.eu-north-1.amazonaws.com", "bedrock-runtime.eu-south-1.amazonaws.com",
    "bedrock-runtime.eu-south-2.amazonaws.com", "bedrock-runtime.us-east-1.amazonaws.com",
    "bedrock-runtime.us-west-2.amazonaws.com", "*.githubcopilot.com", "openrouter.ai",
    "eu.openrouter.ai", "api.mistral.ai", "codestral.mistral.ai", "api.cortecs.ai",
    "api.scaleway.ai", "*.ifr.fr-par.scaleway.com", "*.ifr.nl-ams.scaleway.com", "api.regolo.ai",
    "api.lyceum.technology", "api.elevenlabs.io", "api.eu.residency.elevenlabs.io",
    "api.us.elevenlabs.io", "fal.run", "*.fal.run", "rest.alpha.fal.ai", "api.x.ai",
    "api.groq.com", "api.deepseek.com", "api.together.xyz", "api.together.ai", "api.cohere.com",
    "api.cohere.ai", "api.voyageai.com", "api.deepgram.com", "*.nousresearch.com",
    "api.perplexity.ai", "api.replicate.com", "router.huggingface.co",
    "api-inference.huggingface.co", "*.endpoints.huggingface.cloud", "api.fireworks.ai",
    "api.cerebras.ai", "api.jina.ai", "api.moonshot.ai", "api.moonshot.cn", "api.kimi.com",
    "open.bigmodel.cn", "api.z.ai", "dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
    "*.dashscope.aliyuncs.com", "*.maas.aliyuncs.com", "portal.qwen.ai", "chat.qwen.ai",
    "api.assemblyai.com", "api.eu.assemblyai.com", "streaming.assemblyai.com",
    "streaming.eu.assemblyai.com", "*.api.speechmatics.com", "*.rt.speechmatics.com",
    "api.minimax.io", "api.minimaxi.com", "api.stepfun.ai", "api.stepfun.com",
    "integrate.api.nvidia.com", "api.lkeap.cloud.tencent.com", "tokenhub.tencentmaas.com",
    "api.arcee.ai", "api.gmi-serving.com", "api.actual.inc", "api.upstage.ai",
    "api.tokenfactory.nebius.com", "ollama.com", "opencode.ai", "api.kilo.ai",
    "ai-gateway.vercel.sh", "api.xiaomimimo.com", "api.deepinfra.com", "api.novita.ai",
    "api.krea.ai", "api.router.com", "api.commandcode.ai",
)

#: Entry points (file, dotted function) and the rule each must satisfy.
#: ``gate_first``: the first reference to the gateway comes no later than the
#: first statement that returns a value or constructs a client.
#: ``every_return``: every value-returning statement passes through the gateway.
#: ``references``: the function consults the gateway (its client comes from a
#: gated resolver).
ENTRY_POINTS = {
    "hermes_cli/runtime_provider.py": {
        "resolve_runtime_provider": "gate_first",
        "_resolve_model_gateway_runtime": "gate_first",
    },
    "agent/auxiliary_client.py": {
        "resolve_provider_client": "gate_first",
        "resolve_vision_provider_client": "gate_first",
        "_get_provider_chain": "gate_first",
        "_try_openrouter": "gate_first",
        "_try_nous": "gate_first",
        "_try_custom_endpoint": "gate_first",
        "_resolve_api_key_provider": "gate_first",
        "_try_anthropic": "gate_first",
        "_build_codex_client": "gate_first",
        "_build_xai_oauth_aux_client": "gate_first",
        "_try_azure_foundry": "gate_first",
        "_try_payment_fallback": "gate_first",
        "_get_cached_client": "gate_first",
        "_resolve_gateway_client": "gate_first",
        "_build_call_kwargs": "every_return",
    },
    "run_agent.py": {
        "AIAgent._create_openai_client": "gate_first",
        "AIAgent._model_gateway_client_kwargs": "gate_first",
    },
    "agent/chat_completion_helpers.py": {
        "build_api_kwargs": "every_return",
        "try_activate_fallback": "references",
    },
    "tools/tool_backend_helpers.py": {
        "read_selection": "references",
        "prefers_gateway": "gate_first",
        "managed_nous_tools_enabled": "gate_first",
        "fal_key_is_configured": "gate_first",
    },
    "tools/managed_tool_gateway.py": {
        "resolve_managed_tool_gateway": "gate_first",
        "build_vendor_gateway_url": "gate_first",
    },
    "tools/tts_tool.py": {
        "_get_provider": "gate_first",
        "_resolve_openai_audio_client_config": "gate_first",
    },
    "tools/transcription_tools.py": {
        "_get_provider": "gate_first",
        "_resolve_openai_audio_client_config": "gate_first",
    },
    "agent/image_gen_registry.py": {
        "get_active_provider": "every_return",
        "get_provider": "every_return",
    },
    "agent/video_gen_registry.py": {
        "get_active_provider": "every_return",
        "get_provider": "every_return",
    },
}

#: Same-file helpers that stand for the gateway (each must itself reference it).
GATE_HELPERS = {
    "_resolve_gateway_client", "_try_model_gateway",
    "_resolve_model_gateway_runtime", "_model_gateway_client_kwargs",
    "_check_model_gateway_route", "_apply_model_gateway_identity",
    "_check_tts_model_gateway", "_gateway_stt_provider", "_refuse_under_model_gateway",
    "_model_capability_may_use_nous", "_gateway_checked", "_model_gateway_child_env",
}

CLIENT_CONSTRUCTORS = {
    "OpenAI", "AsyncOpenAI", "_create_openai_client", "build_anthropic_client", "Anthropic",
    "GeminiNativeClient", "CodexAuxiliaryClient", "AnthropicAuxiliaryClient", "Client",
    "urlopen", "post", "request",
}

SDK_CONSTRUCTORS = {"OpenAI", "AsyncOpenAI", "Anthropic", "AsyncAnthropic", "urlopen", "create_connection"}
HTTP_MODULES = {"httpx", "requests", "urllib", "urllib3", "aiohttp", "socket", "openai", "anthropic"}


def _tree(rel):
    return ast.parse((REPO / rel).read_text(encoding="utf-8"), filename=rel)


def _find(tree, dotted):
    parts = dotted.split(".")
    scope = tree.body
    node = None
    for part in parts:
        node = next(
            (n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part),
            None,
        )
        assert node is not None, f"{dotted}: {part} not found"
        scope = node.body
    return node


def _names(node):
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            yield child.id
        elif isinstance(child, ast.Attribute):
            yield child.attr
            if isinstance(child.value, ast.Name):
                yield child.value.id
        elif isinstance(child, (ast.ImportFrom, ast.Import)):
            for alias in child.names:
                yield alias.asname or alias.name
            if isinstance(child, ast.ImportFrom) and child.module:
                yield child.module


def _is_gate(node):
    return any("model_gateway" in name or name in GATE_HELPERS for name in _names(node))


def _own_statements(fn):
    """Top-level statements, skipping the docstring and nested function defs."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    return [s for s in body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _trivial_return(node):
    value = node.value
    if value is None:
        return True
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, ast.Tuple) and all(isinstance(e, ast.Constant) for e in value.elts):
        return True
    return False


def _returns(stmt):
    stack = list(stmt.body) if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) else [stmt]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Return):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _egress(stmt):
    for ret in _returns(stmt):
        if not _trivial_return(ret):
            return True
    for child in ast.walk(stmt):
        if isinstance(child, ast.Call):
            func = child.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name in CLIENT_CONSTRUCTORS:
                return True
    return False


def _cases():
    for rel, functions in ENTRY_POINTS.items():
        for dotted, rule in functions.items():
            yield pytest.param(rel, dotted, rule, id=f"{rel}::{dotted}")


def _violation(fn, rule):
    """None when ``fn`` satisfies ``rule``, else the reason."""
    name = fn.name
    statements = _own_statements(fn)
    if not statements:
        return f"{name} has no body"
    if rule == "references":
        return None if _is_gate(fn) else f"{name} never consults model_gateway"
    if rule == "every_return":
        value_returns = [r for r in _returns(fn) if not _trivial_return(r)]
        if not value_returns:
            return f"{name} returns nothing"
        for ret in value_returns:
            if not _is_gate(ret):
                return f"{name}: the return at line {ret.lineno} bypasses model_gateway"
        return None
    gate_index = next((i for i, s in enumerate(statements) if _is_gate(s)), None)
    if gate_index is None:
        return f"{name} never consults model_gateway"
    egress_index = next((i for i, s in enumerate(statements) if _egress(s)), len(statements))
    if gate_index > egress_index:
        return (
            f"{name}: line {statements[egress_index].lineno} can return a client or backend "
            f"before the gateway check at line {statements[gate_index].lineno}"
        )
    return None


@pytest.mark.parametrize("rel,dotted,rule", list(_cases()))
def test_entry_point_consults_the_gateway_first(rel, dotted, rule):
    assert _violation(_find(_tree(rel), dotted), rule) is None


def test_the_scan_catches_a_bypass():
    bypass = ast.parse(
        "def resolve(provider):\n"
        "    if provider == 'openrouter':\n"
        "        return OpenAI(api_key='k', base_url='https://openrouter.ai/api/v1')\n"
        "    if _model_gateway.enforced():\n"
        "        return None\n"
    ).body[0]
    assert "before the gateway check" in _violation(bypass, "gate_first")
    unguarded = ast.parse("def build(kwargs):\n    if kwargs:\n        return kwargs\n    return dict(kwargs)\n").body[0]
    assert "bypasses" in _violation(unguarded, "every_return")
    assert "never consults" in _violation(unguarded, "references")
    guarded = ast.parse(
        "def resolve(provider):\n"
        "    if _model_gateway.enforced():\n"
        "        return _resolve_gateway_client(provider)\n"
        "    return OpenAI(api_key='k')\n"
    ).body[0]
    assert _violation(guarded, "gate_first") is None


@pytest.mark.parametrize("rel", sorted(ENTRY_POINTS) + ["tools/terminal_tool.py"])
def test_gate_helpers_consult_the_gateway(rel):
    tree = _tree(rel)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in GATE_HELPERS:
            names = set(_names(node)) - {node.name}
            assert any("model_gateway" in n or n in GATE_HELPERS for n in names), f"{rel}::{node.name}"


def test_local_terminal_children_get_the_gateway_environment():
    fn = _find(_tree("tools/terminal_tool.py"), "_create_environment")
    local_branch = next(
        s for s in _own_statements(fn)
        if isinstance(s, ast.If) and "local" in ast.unparse(s.test) and "env_type" in ast.unparse(s.test)
    )
    ret = next(_returns(ast.Module(body=local_branch.body, type_ignores=[])))
    assert "_model_gateway_child_env" in ast.unparse(ret)


def _string_literals(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _host_matches(pattern, text):
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return any(token.endswith(suffix) for token in text.replace("/", " ").split())
    return pattern in text


def test_the_gateway_module_names_no_provider_host_or_client():
    tree = _tree("agent/model_gateway.py")
    literals = list(_string_literals(tree))
    for pattern in PROVIDER_HOSTS:
        hits = [s for s in literals if _host_matches(pattern, s)]
        assert not hits, f"model_gateway names provider host {pattern}: {hits}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in SDK_CONSTRUCTORS, f"model_gateway calls {func.id} at line {node.lineno}"
            elif isinstance(func, ast.Attribute):
                assert func.attr not in SDK_CONSTRUCTORS, f"model_gateway calls {func.attr} at line {node.lineno}"
                root = func.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name):
                    assert root.id not in HTTP_MODULES, f"model_gateway calls {root.id} at line {node.lineno}"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            modules = [module] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
            for name in modules:
                assert name == "urllib.parse" or name.split(".")[0] not in HTTP_MODULES, name


_KEY_SUFFIXES = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET")
_READERS = {"getenv", "get", "get_secret", "_scoped_env"}


def test_the_gateway_module_reads_no_provider_key():
    tree = _tree("agent/model_gateway.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        first = node.args[0]
        if name in _READERS and isinstance(first, ast.Constant) and isinstance(first.value, str):
            var = first.value
            if var.endswith(_KEY_SUFFIXES):
                assert var.startswith("SP_"), f"model_gateway reads {var} at line {node.lineno}"


# ---------------------------------------------------------------------------
# Socket guard: every entry point under enforcement, no other connection
# ---------------------------------------------------------------------------


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
