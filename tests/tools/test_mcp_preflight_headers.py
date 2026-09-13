"""Header diagnosis must not drain a valid, intentionally open MCP stream."""

import asyncio
import json
import time

import httpx
import pytest

from tools import mcp_tool


@pytest.mark.parametrize("fallback", ["GET", "POST"])
def test_open_loopback_stream_returns_before_body_finishes(fallback):
    """Real HTTPX + TCP: hold SSE open until the probe closes its connection."""
    async def scenario():
        methods, handlers, closed = [], set(), asyncio.Event()

        async def serve(reader, writer):
            current = asyncio.current_task()
            handlers.add(current)
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                method = request.split(b" ", 1)[0].decode()
                methods.append(method)
                headers = dict(line.lower().split(b": ", 1) for line in request.split(b"\r\n")[1:] if b": " in line)
                if b"content-length" in headers:
                    body = await reader.readexactly(int(headers[b"content-length"]))
                    assert json.loads(body)["method"] == "initialize"
                if method == "HEAD":
                    status = b"405 Method Not Allowed" if fallback == "GET" else b"200 OK"
                    writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/html\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                else:
                    assert method == fallback
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                    await writer.drain()
                    # No body bytes, terminating chunk, or peer EOF is sent.
                    # Header-only validation must proactively close us.
                    assert await reader.read() == b""
                    closed.set()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(current)

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        started = time.monotonic()
        try:
            # Broad scheduling allowance, still below the actual 5s probe
            # budget. The old implementation waits for its full budget.
            await asyncio.wait_for(
                mcp_tool.MCPServerTask("synthetic")._preflight_content_type(
                    f"http://127.0.0.1:{port}/mcp", timeout=5), 2.5)
            await asyncio.wait_for(closed.wait(), 3)
            assert methods == ["HEAD", fallback]
            print(f"header_probe_{fallback.lower()}_seconds={time.monotonic() - started:.6f}")
        finally:
            server.close()
            await server.wait_closed()
            for task in tuple(handlers):
                task.cancel()
            await asyncio.gather(*tuple(handlers), return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("fallback", ["GET", "POST"])
@pytest.mark.parametrize("status,ctype,rejected", [
    (200, "text/event-stream", False), (200, "application/json", False),
    (200, "text/html", True), (401, "text/html", False),
    (503, "text/plain", False), (200, "", False),
])
def test_fallback_headers_preserve_decision_without_reading_body(
    monkeypatch, fallback, status, ctype, rejected,
):
    streams, methods = [], []

    class Body(httpx.AsyncByteStream):
        consumed = False
        closed = False

        async def __aiter__(self):
            self.consumed = True
            yield b"arbitrary content which the header probe must not need"

        async def aclose(self):
            self.closed = True

    async def handler(request):
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405 if fallback == "GET" else 200,
                                  headers={"content-type": "text/html"})
        stream = Body()
        streams.append(stream)
        return httpx.Response(status, headers={"content-type": ctype}, stream=stream)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(**kw, transport=httpx.MockTransport(handler)))

    async def scenario():
        probe = mcp_tool.MCPServerTask("synthetic")._preflight_content_type("https://synthetic.invalid/mcp")
        # On POST fallback, a non-2xx or missing type cannot overturn the
        # original definite HTML result. Preserve that existing decision.
        expect_reject = rejected or (fallback == "POST" and (status != 200 or not ctype))
        if expect_reject:
            with pytest.raises(mcp_tool.NonMcpEndpointError):
                await probe
        else:
            await probe

    asyncio.run(scenario())
    assert all(stream.closed and not stream.consumed for stream in streams)
    assert methods[:2] == ["HEAD", fallback]
