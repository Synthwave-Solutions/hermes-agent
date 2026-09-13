# Explicit router context reconciliation

An existing persistent context value made an explicit OmniRoute agent probe
`/v1/models/{model}` before its models catalog. The deployed route manifest
contains the catalog route and no model-detail route. Both the cold and warm
own QA constructors spent approximately three seconds in context setup. A
detail read timeout exits the probe's existing exception boundary before the
catalog request, leaving the old context value in place.

For the existing explicit remote-router mode only, reconciliation now goes
directly to the standard models catalog. It retains the native model matching,
runtime context parsing, explicit overrides, changed/subminimum window handling,
failure fallback and cancellation behavior. Native/local servers retain their
hardware and per-model runtime probes. The chosen model, reasoning and provider
configuration are unchanged.

The existing positive-only, 30-second probe snapshot now includes the active
profile and credential fingerprint for this router mode. No new cache, disk
snapshot, authorization decision, background worker or longer TTL is added.
Metadata is not an access grant; request authorization continues independently.

Validation uses a real AIAgent constructor and hermetic HTTP transports. The
unsupported detail request consumes an injected three-second transport budget;
the catalog returns a changed context window. The original source produces six
failures and two passes; the corrected source passes all eight cases, covering
profile/credential separation, the existing TTL, failed-probe recovery, explicit
overrides, subminimum limits and external cancellation. Neighbor fixtures still
exercise native protocol detection and update the router's expected request to
the supported catalog. Injected transport time is not a production benchmark.

The source fix needs an independent review and a real post-release own-chat
measurement before claiming a user-visible latency improvement.
