# Startup latency: schema capability lookup

Browser tool descriptions previously resolved native image routing through the same network-enabled capability lookup used when an image is actually submitted. A cold text-only turn could therefore wait for models.dev or endpoint discovery merely to choose introductory tool wording.

Schema construction now requests cached capabilities only. Explicit configuration overrides and cached capabilities remain authoritative. On an unknown cache result, the description uses neutral guidance rather than claiming the model cannot view images. Actual image and screenshot execution still uses the existing network-enabled routing path. No tools, permissions, profile credentials, model selection, or context limits are changed.

## Evidence and limits

A production function-stack sample spent approximately eight seconds in this description lookup during a cold no-image turn. A controlled metadata-delay benchmark (500 ms injected only when network is allowed) measured the prior helper at500.4 ms and the new description path at0.287 ms; the network flags were True then False. This isolates the avoided network dependency, not a predicted whole-turn speedup.

Canonical tests cover cold schemas avoiding catalog and local endpoint probes, unchanged actual image routing, and preserved explicit vision configuration. Existing image-routing and browser execution tests remain in scope. Cold MCP connections, imports, and provider processing still have separate costs; a subsequent native cold/warm measurement is needed before claiming an overall improvement.

## Vision tool availability

The separate `check_vision_requirements()` path already used `aux_probe_mode()`
to avoid constructing SDK clients, but its main-model capability guard still
enabled network lookup. Unknown local custom models could trigger the server
fingerprint waterfall; cold catalog-backed providers could fetch models.dev,
and stale catalogs could start background refreshes merely to list tools.

The guard now passes `allow_network=False` only while that thread's availability
probe is active. Explicit and cached false/true capability decisions retain
their meaning. An unknown capability still allows the historical attempt;
actual image resolution outside probe mode retains live discovery and its
text-only model guard. Provider selection, credentials, grants and model
defaults are unchanged.

`tests/tools/test_vision_availability_no_network.py` runs the real requirements,
auxiliary resolver and image-routing chain against synthetic failed and gated
hung HTTP boundaries, checks stale cache and explicit configuration, and
verifies runtime resolution plus nested/error and concurrent-thread isolation.
This removes a demonstrated startup dependency, not all possible provider
availability I/O: provider-specific default-model hooks and MCP discovery are
separate paths. Whole-turn latency still requires a new production measurement.

## Ollama num_ctx detection during initialization

The later Ollama `num_ctx` safeguard used the full local-server fingerprint
waterfall even when an explicit context length was configured. A stack-only
sample of one production cold worker attributed about 7.95 seconds to HTTP
response-header waits under `query_ollama_num_ctx` / `detect_local_server_type`.
Those sampled weights identify the blocking path; they are not precise phase
durations or a promised whole-turn speedup.

Only `query_ollama_num_ctx` now requests Ollama-only detection: on an unknown
endpoint it checks `/api/tags` once, using the existing 2-second HTTP timeout,
and skips the LM Studio, llama.cpp and vLLM fingerprint requests. Custom Ollama
behind a reverse proxy or arbitrary port remains discoverable. Known server
cache/disk verdicts, `/api/show`, Modelfile `num_ctx`, the explicit configuration
override, VRAM cap and compressor-window clamp retain their existing behavior.
The default full detector is unchanged for callers that need all server types.

A targeted miss is stored only in a separate in-memory cache: at most 256 entries,
with the existing 300-second failure TTL. Its identity hashes the active profile,
normalized endpoint and API key; no raw credential is kept in the cache or
persisted. Credential rotation or a different profile gets a fresh check.
Newer positive generic/disk verdicts take precedence. Targeted misses never become
generic server-type negatives, and the cache lock is never held during HTTP.
The 2-second timeout bounds each HTTP stage, not a strict total DNS/connect/read
wall-clock deadline. Missing results can be retried after expiry; no new retries
or model requests are introduced.

`tests/test_agent_init_ollama_probe_scope.py` exercises the real initializer and
compressor against synthetic HTTP, including a slow generic gateway, reverse-proxy
Ollama, cached and replaced servers, explicit/Modelfile context caps, bounded and
concurrent cache use, profile/key separation and generic detection after a miss.
Production acceptance still requires a new cold/warm measurement; MCP startup,
provider latency and post-answer settlement are separate costs.
