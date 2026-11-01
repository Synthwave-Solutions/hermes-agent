# Rechecking a known router context window

An explicit `custom:omniroute` route can already have a valid positive context
window on disk while its optional models catalog is unresponsive. Revalidating
that value previously spent the normal three-second HTTPX read timeout on each
new agent before returning the unchanged cached window. A production diagnostic
confirmed that the catalog GET itself timed out, with native protocol probes
disabled and without a proxy; this was not a failed model-detail route.

Only reconciliation of an already-positive cached router window now uses HTTPX
connect/read/write/pool timeouts of 0.5 seconds. This is a per-operation timeout,
not a global elapsed-time guarantee for a server that continuously trickles data.
The existing cached fallback, configured overrides and minimum-window checks
remain intact. A timely lower live window still replaces or invalidates the old
window. Cold discovery without a cached value and genuine native/local providers
keep their existing timeouts and probe paths. Model selection and reasoning
settings are unchanged.

The shorter optional check never creates an endpoint-blackhole entry and never
caches a failed result. A following lookup can therefore recover immediately,
including cold discovery with its normal budget. Successful values retain the
existing positive-only 30-second, profile/credential-scoped router probe cache.
No background threads, negative cache or persistent credential state are added.

Validation uses the real context resolver and HTTPX against an inert localhost
HTTP server, plus hermetic transports for the remaining boundaries. The initial
baseline spent 3.03 seconds on the idle catalog fixture; the revised check passes
the generous two-second assertion and then immediately learns a recovered live
window. These are local tests; production latency must be measured after release.
