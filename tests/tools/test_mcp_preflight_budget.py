"""Exercise the real preflight and connection path without network access."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from tools import mcp_tool


def _mock_http(monkeypatch, handler):
    """Keep real HTTP clients/requests, replace only their network transport."""
    clients, options, transports = [], [], []
    real_client = httpx.AsyncClient

    class Transport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True
            await super().aclose()

    def client(**kwargs):
        options.append(kwargs.copy())
        transport = Transport(handler)
        transports.append(transport)
        instance = real_client(**kwargs, transport=transport)
        clients.append(instance)
        return instance

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return clients, options, transports


@pytest.mark.parametrize("head_fallback", [False, True])
def test_preflight_budget_is_shared_by_all_requests(monkeypatch, head_fallback):
    """Each reply fits the budget; their cumulative delay does not."""
    cancelled, completed = [], []
    delay = 0.4 if head_fallback else 0.6

    async def handler(request):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            cancelled.append(request.method)
            raise
        completed.append(request.method)
        if request.method == "HEAD" and head_fallback:
            return httpx.Response(405)
        content_type = "application/json" if request.method == "POST" else "text/html"
        return httpx.Response(200, headers={"content-type": content_type})

    clients, _, transports = _mock_http(monkeypatch, handler)

    async def scenario():
        server = mcp_tool.MCPServerTask("budget-test")
        await asyncio.wait_for(
            server._preflight_content_type("https://mcp.example.invalid", timeout=1.0),
            timeout=5.0,
        )

    asyncio.run(scenario())
    # No tight wall-clock assertion: scheduler delay may expire the shared
    # budget in an earlier request, which is still the correct behavior.
    assert len(cancelled) == 1
    assert "POST" not in completed
    assert all(client.is_closed for client in clients)
    assert all(transport.closed for transport in transports)


def _connection_fixture(monkeypatch):
    """Use native run/HTTP/negotiate/discover; fake only SDK wire objects."""
    calls = []
    tool = SimpleNamespace(name="technical_status", description="Fixture", inputSchema={})

    @asynccontextmanager
    async def wire(*args, **kwargs):
        calls.append("transport")
        yield None, None

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            calls.append("initialize")
            return SimpleNamespace(capabilities=SimpleNamespace(tools=SimpleNamespace()))

        async def list_tools(self, **kwargs):
            calls.append("tools/list")
            return SimpleNamespace(tools=[tool], nextCursor=None)

    async def finish(server):
        assert server._ready.is_set()
        assert server._tools == [tool]
        server._shutdown_event.set()
        return "shutdown"

    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: True)
    monkeypatch.setattr(mcp_tool, "_MCP_HTTP_AVAILABLE", True)
    monkeypatch.setattr(mcp_tool, "_MCP_NEW_HTTP", True)
    monkeypatch.setattr(mcp_tool, "sdk_httpx", lambda: httpx)
    monkeypatch.setattr(mcp_tool, "streamable_http_client", wire)
    monkeypatch.setattr(mcp_tool, "ClientSession", Session)
    monkeypatch.setattr(mcp_tool.MCPServerTask, "_wait_for_lifecycle_event", finish)
    return calls


@pytest.mark.parametrize("external_cancel", [False, True])
def test_post_probe_timeout_reaches_handshake_but_external_cancel_does_not(
    monkeypatch, external_cancel,
):
    entered, cancelled = asyncio.Event(), []
    methods = []

    async def handler(request):
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/html"})
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.append(request.method)
            raise

    clients, _, transports = _mock_http(monkeypatch, handler)
    calls = _connection_fixture(monkeypatch)
    native_probe = mcp_tool.MCPServerTask._preflight_content_type

    async def shorter_probe(server, url, **kwargs):
        await native_probe(server, url, timeout=0.1 if not external_cancel else 10.0, **kwargs)

    monkeypatch.setattr(mcp_tool.MCPServerTask, "_preflight_content_type", shorter_probe)

    async def scenario():
        server = mcp_tool.MCPServerTask("connection-test")
        task = asyncio.create_task(server.run({
            "url": "https://mcp.example.invalid",
            "sampling": {"enabled": False},
            "elicitation": {"enabled": False},
        }))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5.0)
            if external_cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not server._ready.is_set()
            else:
                await asyncio.wait_for(task, timeout=5.0)
                assert server._error is None
                assert server._ever_connected
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())
    assert methods == ["HEAD", "GET", "POST"]
    assert cancelled == ["POST"]
    assert calls == ([] if external_cancel else ["transport", "initialize", "tools/list"])
    assert all(client.is_closed for client in clients)
    assert all(transport.closed for transport in transports)


@pytest.mark.parametrize("head_status", [200, 405, 501])
@pytest.mark.parametrize("post_type", ["text/html", "application/json", "text/event-stream"])
def test_fast_fallback_keeps_endpoint_validation(monkeypatch, head_status, post_type):
    methods = []

    async def handler(request):
        methods.append(request.method)
        content_type = post_type if request.method == "POST" else "text/html"
        status = head_status if request.method == "HEAD" else 200
        return httpx.Response(status, headers={"content-type": content_type})

    _mock_http(monkeypatch, handler)

    async def scenario():
        server = mcp_tool.MCPServerTask("validation-test")
        if post_type == "text/html":
            with pytest.raises(mcp_tool.NonMcpEndpointError, match="validation-test"):
                await server._preflight_content_type("https://mcp.example.invalid")
        else:
            await server._preflight_content_type("https://mcp.example.invalid")

    asyncio.run(scenario())
    assert methods == (["HEAD", "POST"] if head_status == 200 else ["HEAD", "GET", "POST"])


def test_probe_forwards_headers_and_tls_settings(monkeypatch):
    received = []

    async def handler(request):
        received.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"})

    _, options, _ = _mock_http(monkeypatch, handler)
    certificate = ("fixture-client.pem", "fixture-key.pem")
    asyncio.run(mcp_tool.MCPServerTask("tls-test")._preflight_content_type(
        "https://mcp.example.invalid", headers={"X-Fixture-Identity": "synthetic"},
        ssl_verify=False, client_cert=certificate,
    ))
    assert options[0]["verify"] is False
    assert options[0]["cert"] == certificate
    assert received[0].headers["X-Fixture-Identity"] == "synthetic"


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_errors_remain_best_effort(monkeypatch, error_type):
    async def handler(request):
        raise error_type("synthetic transport error", request=request)

    clients, _, _ = _mock_http(monkeypatch, handler)
    asyncio.run(mcp_tool.MCPServerTask("transport-test")._preflight_content_type(
        "https://mcp.example.invalid",
    ))
    assert all(client.is_closed for client in clients)


def test_unrelated_timeout_error_still_propagates(monkeypatch):
    async def handler(request):
        raise TimeoutError("not the probe deadline")

    _mock_http(monkeypatch, handler)
    with pytest.raises(TimeoutError, match="not the probe deadline"):
        asyncio.run(mcp_tool.MCPServerTask("unrelated-timeout")._preflight_content_type(
            "https://mcp.example.invalid",
        ))


@pytest.mark.parametrize("gate", [{"auth": "oauth"}, {"transport": "sse"}, {"skip_preflight": True}])
def test_run_preflight_skip_gates_are_unchanged(monkeypatch, gate):
    reached = []

    async def unexpected_http(request):
        raise AssertionError("A skipped probe must not send a request")

    _mock_http(monkeypatch, unexpected_http)

    async def stop_at_sdk(server, config):
        reached.append(config)
        server._shutdown_event.set()
        return "shutdown"

    monkeypatch.setattr(mcp_tool, "_ensure_mcp_sdk", lambda: True)
    monkeypatch.setattr(mcp_tool.MCPServerTask, "_run_http", stop_at_sdk)
    asyncio.run(mcp_tool.MCPServerTask("skip-test").run({
        "url": "https://mcp.example.invalid", "sampling": {"enabled": False},
        "elicitation": {"enabled": False}, **gate,
    }))
    assert len(reached) == 1
