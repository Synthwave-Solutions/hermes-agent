# Targeted LM Studio metadata detection

SynthPulse's cold custom-provider initialization was observed waiting in
`ContextCompressor.context_length → fetch_endpoint_model_metadata →
detect_local_server_type`. That metadata caller only needs to know whether
LM Studio's loaded-instance context must precede the OpenAI-compatible model
listing, but it previously probed Ollama, llama.cpp and vLLM as well.

The caller now selects an LM Studio-only probe. Known positive memory/disk
verdicts still take precedence. A native LM Studio loaded context remains
authoritative over a catalog maximum. Rich standard model metadata can then
resolve context without the unrelated protocol waterfall. The later Ollama
`num_ctx` safeguard, model selection, reasoning and tool/governance rules stay
unchanged. A default model's context pin still cannot cross a model or route
boundary.

A targeted miss is memory-only, bounded to 256 entries and expires after the
existing failure TTL. It is keyed by a digest of probe kind, current profile,
endpoint and credential, and never becomes a generic negative verdict. The
cache lock is released before any HTTP. Later positive discovery wins.

When standard metadata is missing, the existing full local-context fallback
still runs; that path may repeat the LM Studio probe. This change therefore
removes avoidable probes on a successful standard metadata path, and does not
promise a global initialization deadline. HTTP read timeouts are inactivity
budgets, not hard wall-time limits.

Validation uses the real agent constructor/compressor with temporary profiles
and intercepted HTTP, including native loaded-context precedence, real Ollama
runtime `num_ctx`, current ContextVar profile scopes and concurrent requests.
No production provider call or production configuration write is part of these
tests. Live timing after deployment must be measured separately.
