# SynthPulse endpoint metadata cache

On base engine `0017f58ea42a80e9661b57e07e88c7bfe022a519`, concurrent callers of
`fetch_endpoint_model_metadata` independently requested the same catalog. Its
URL-only cache also returned one profile's or API key's context/pricing snapshot
to another. Four regressions reproduced both behaviors using native imports,
real `requests`/`httpx` clients and a local HTTP server under temporary Hermes
homes. No external provider or production configuration was used.

The in-memory snapshot and in-flight request now use a SHA-256 identity covering
the active context-local Hermes home, normalized endpoint and supplied credential.
Raw credentials are neither retained in cache keys nor persisted. Snapshots retain
the existing 300-second TTL and are bounded to 256 entries. Concurrent requests
within one identity share one probe; different identities proceed independently.
The lock covers state transitions only. No network or waiter executes under it.

`cached_only=True` returns immediately without starting or joining a request.
An explicit refresh bypasses a saved snapshot and joins any already-running
fresh probe for that identity. Ordinary reads may still use a valid saved
snapshot during refresh. Followers stop waiting after 60 seconds and return
unknown metadata without cancelling or duplicating the leader. A failed or
interrupted leader always releases followers; a later caller can retry. Existing
HTTP timeouts, authentication failure handling and probe order remain unchanged.

The patch does not change prompt caching, conversation history, model selection,
tool availability, MCP retries, governance decisions, persistent context-cache
rules or LM Studio/Ollama runtime context limits. It fixes this catalog cache;
it does not claim that every other metadata cache has been audited.

## Validation

The same six-call local HTTP experiment used a serialized 150-millisecond catalog
handler. Before: six `/v1/models` requests, 1.0079 seconds. After: one request,
0.2549 seconds. These are local concurrency measurements, not production latency
estimates. Timing is recorded as evidence; correctness assertions use request
counts, actual results and event synchronization rather than tight timing limits.

Sixteen focused tests cover profile/key/endpoint isolation, actual parallel HTTP,
cached-only behavior, concurrent refresh, waiter timeout, failed/interrupted
leaders, 401/403 recovery, bounded TTL storage and the real AIAgent context plus
usage-accounting path. All 223 affected neighboring tests also pass, including
custom TLS, endpoint blackholes, dynamic local context and the prior LM Studio
probe correction. Commands used the required hermetic wrapper:

```sh
scripts/run_tests.sh -j 4 tests/agent/test_endpoint_metadata_singleflight.py
scripts/run_tests.sh -j 4 tests/agent/test_model_metadata.py tests/agent/test_model_metadata_local_ctx.py tests/agent/test_endpoint_blackhole.py tests/agent/test_probe_cache_followups.py tests/agent/test_custom_provider_ca_probes.py tests/test_agent_init_lmstudio_probe_scope.py tests/agent/test_nonblocking_usage_accounting.py
```

## Production acceptance still required

Root owns rollout and live testing. After exact source/deployment readback, use
the existing actual-frontend QA: confirm the selected custom OmniRoute Astra
route, High reasoning and Super mode before submit; record process-cold provenance,
the six existing preparation markers, gateway timings and actual completed output.
Repeat warm and one-tool cases. To attribute a speed change to this patch, observe
overlapping same-identity metadata consumers and their request counts; an isolated
single caller cannot demonstrate a coalescing benefit. Retain the prior measured
17.3-second constructor and 9.6-second MCP preparation as unresolved until the
same live measurement proves otherwise. The existing MCP audit found real retry
waits, not an unnecessary terminal sleep; this patch leaves that behavior intact.
