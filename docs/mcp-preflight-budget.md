# MCP content-type preflight budget

The optional Streamable HTTP content-type probe now shares its existing
five-second budget across HEAD, the GET fallback and the initialize POST
fallback. Previously, each HTTP I/O phase could consume its own timeout,
allowing several sequential waits before the actual MCP handshake began.

A single `asyncio.timeout` scope surrounds the probe client lifetime. Budget
expiry cancels outstanding probe I/O and returns to the normal SDK handshake,
just as an HTTP transport timeout already did. It does not validate an
endpoint, authenticate a user or publish tools. SDK initialization and tool
discovery must still complete. Client cleanup remains cooperative; this is
not a promise to interrupt blocking or cancellation-resistant third-party
code within a strict wall-clock limit.

Fast, definite non-MCP responses still raise `NonMcpEndpointError`, including
HTML after an unsuccessful POST fallback. Valid POST-only endpoints remain
supported. A timeout cannot turn an earlier HTML response into an endpoint
rejection. External cancellation and unrelated `TimeoutError` exceptions
still propagate. Headers, TLS settings, OAuth/SSE skip gates, tool schemas,
governance, credentials, providers and model configuration are unchanged.

## Local verification

Run through the repository's isolated test runner:

```sh
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh \
  tests/tools/test_mcp_preflight_budget.py \
  tests/tools/test_mcp_preflight_content_type.py \
  tests/tools/test_mcp_initial_connect_shutdown.py \
  tests/tools/test_mcp_failure_classification.py \
  tests/tools/test_mcp_capability_gating.py \
  tests/tools/test_mcp_sse_transport.py \
  tests/tools/test_mcp_stdio_init_timeout.py \
  tests/tools/test_mcp_tool.py -j 3 -q
```

The new tests import the native preflight, run loop, HTTP connection,
negotiation and discovery methods. Real httpx clients use an in-memory
transport; SDK wire/session boundaries use synthetic fixtures. No provider,
production configuration or external server is involved. Tests cover shared
cumulative delay, a stalled POST, cleanup, handshake continuation, external
cancellation, rapid HTML rejection, successful POST fallback, HTTP errors,
header/TLS forwarding and skip gates. They use events and outcome assertions
instead of narrow elapsed-time thresholds.

On the unfixed engine, the two cumulative-delay cases complete without a
deadline and the stalled-POST case never reaches initialization. These three
regressions fail before the fix. Production latency improvement remains
unmeasured: the earlier 10.5-second MCP initialization sample does not identify
an individual server or transport phase, so it cannot establish that this
probe caused that specific delay.
