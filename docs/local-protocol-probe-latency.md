# Slow optional server discovery during context reconciliation

A preexisting context-cache entry takes a different path: it checks whether a
local server's actual allocation changed before accepting the cached value.
This path can run four optional protocol probes before reading model details;
per-process metadata caching alone does not remove that cost.

When the first LM Studio probe specifically hits a read timeout, the remaining
Ollama, llama.cpp and vLLM checks now run concurrently in one joined executor.
Their results and connection-failure effects are applied in the original
priority order. Per-request timeouts, endpoints, native loaded-context queries
and cached-context reconciliation remain unchanged. A first-probe success,
connect timeout, responsive nonmatching endpoint and targeted one-protocol
checks retain their original sequential/early-exit behavior. All probe workers
finish before the shared HTTP client closes; no background work escapes.

For the observed pattern of four two-second read timeouts, this overlaps the
last three waits. It does not speed up MCP startup or inference. A slow first
probe followed by an immediately valid Ollama endpoint can now wait for the
other concurrent checks to finish; this bounded tradeoff keeps cleanup joined
and native result precedence deterministic.
