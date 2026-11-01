# Explicit remote router metadata

When the current provider is explicitly `custom:omniroute`, constructor metadata discovery skips native LM Studio, Ollama, llama.cpp and vLLM hardware probes. The preserved requested identity may identify that route only while the resolved provider is canonical `custom` or `openai`; an actual native or fallback provider keeps its own behavior. Loopback alone is not treated as evidence of a router.

The same context reconciliation still reads the model detail endpoint and falls back to the standard models catalog. Explicit context configuration retains precedence, changed and undersized live windows remain visible to the existing minimum-context guard, and an explicitly configured Ollama allocation still applies its existing clamp. Model, reasoning effort, selected provider, credentials, tools and prompt contents are unchanged.

The existing in-memory caches distinguish native-probe and remote-router results. No durable catalog cache or MCP registry shortcut is introduced. A compressor model update drops its old metadata identity so that later native fallback cannot inherit the router optimization.

Native constructor regressions reproduce the unnecessary hardware requests on the frozen base and prove they are absent on this candidate while the live model window remains 98304. Mock transports cover catalog fallback, explicit overrides, native-provider behavior and cache-mode separation. These are local tests; production startup latency must be measured after rollout. MCP cold-start discovery remains a separate investigation.
