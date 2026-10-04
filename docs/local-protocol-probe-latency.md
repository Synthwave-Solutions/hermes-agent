# Slow optional server discovery during context reconciliation

A preexisting context-cache entry takes a different path: it checks whether a
local server's actual allocation changed before accepting the cached value.
This path can run four optional protocol probes before reading model details;
per-process metadata caching alone does not remove that cost.

When the first LM Studio probe specifically hits a read timeout, the remaining
Ollama, llama.cpp and vLLM checks now run concurrently in one owned async loop.
Their results and connection-failure effects are applied in the original
priority order. Per-request timeouts, endpoints, native loaded-context queries
and cached-context reconciliation remain unchanged. A first-probe success,
connect timeout, responsive nonmatching endpoint and targeted one-protocol
checks retain their original sequential/early-exit behavior. Once an earlier
priority probe supplies a match or connection timeout, later checks are
cancelled and drained before the async client closes and its loop worker joins.
This also closes a lower-priority response that keeps trickling bytes within
HTTPX's inactivity timeout; no background work escapes.

Each speculative probe has a three-second total budget covering headers, body
and any llama.cpp fallback. If an incomplete probe is actually needed, detection
resumes the original serial behavior from that probe. This avoids treating a
slow native server as a negative match or changing its loaded context limit.

For the observed pattern of four two-second read timeouts, this overlaps the
last three waits. It does not speed up MCP startup or inference and is not a
global four-second deadline: the existing llama.cpp fallback and a needed
incomplete probe can still take additional time.
