# MCP preflight response headers

The optional Streamable HTTP endpoint check needs status and Content-Type,
not a response body. After HEAD 405/501 it used buffered GET, and for HTML
endpoints it used buffered initialize POST. A valid SSE response intentionally
keeps its body open, so either request could spend the entire existing five
second probe budget before the real SDK connection began.

GET and POST now use HTTPX streaming response contexts and close immediately
after receiving headers. The real SDK still initializes the session and lists
tools. Request methods, headers, client certificates, TLS verification,
redirect behavior, OAuth/SSE/explicit skip gates, content-type decisions and
the shared probe deadline are unchanged. This does not cache schemas,
credentials, authorization decisions or clients, or alter profile/toolset
selection, governance, the discovery lock or connection concurrency.

Two real local TCP tests hold an SSE body open after GET or POST and require
the probe to finish before its deadline and close the connection. Twelve
native HTTPX transport cases preserve status/type decisions without consuming
any body. The existing 34 probe/deadline/connection tests remain applicable.

This removes a demonstrated optional wait for these response shapes. The
observed production cold discovery duration of 9.856 seconds is a separate
measurement; its exact decomposition is not established by these tests.
Handshakes, SDK import, plugin/config work, stdio startup and lock contention
can still take time. No live performance result is claimed here.
