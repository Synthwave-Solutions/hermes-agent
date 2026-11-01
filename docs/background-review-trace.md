# Background review diagnostics

The existing `agent.background_review` INFO logger emits structured local records
with the prefix `Background review trace: `. This diagnostic does not change
review eligibility, prompts, provider selection, tool permissions, cancellation,
skill creation or chat cards. It adds no logging handler or telemetry endpoint.

Each record contains an event, the SHA-256 of the session ID and whether the
emitter is a review fork. Scheduled reviews also carry a random opaque review ID.
The schema accepts only fixed boolean/count fields and enumerated reasons and
results. It never includes messages, prompts, credentials, provider errors,
profile identities, paths or skill names.

The finalizer's `gate` record captures actual skill-tool availability, interval,
counter before the existing reset, memory/skill triggers, response presence,
interrupt/skip flags and the existing scheduling condition. That condition can
still be declined by the spawn wrapper (`disabled`, `delegated` or
`reservation_unavailable`) or the worker (`provider_cannot_use_tools`). The counter
tracks eligible conversation iterations, not individual calls within a parallel
tool batch. The gate occurs at turn finalization; starting a new chat is not a
requirement.

The native worker emits `scheduled`, `started`, `request_started` and its observed
`completed`, `failed` or `cancelled` outcome. `cancel_requested` describes the
existing live-turn cancellation handshake. Cancellation classification covers
that handshake and the native interrupted result, not every possible
`BaseException`. Startup failures retain their original exception behavior.
`completed` with result `none` is a review that produced no classified writes;
it is not a skill-created event. Confirmed mutation observers and their existing
private-owner UI rules remain separate.

These records use the existing logging level, routing and retention. The native
rotating file handler retains worker records while thread-scoped console silence
is active; a console handler alone may suppress those worker records. Production
acceptance must confirm that a real gate and lifecycle reach the configured
log destination. Logging errors are best effort and cannot block the review, so
an absent trace alone does not prove a skip. Historical runs made before this
instrumentation cannot be assigned a retrospective skip reason.

Focused tests exercise the real finalizer, parent/fork lifecycle, a synthetic
HTTP response through the native provider loop, concurrent reservation/live-turn
cancellation and rotating file output during actual worker silence. They use a
temporary profile and do not send external requests or force skill mutations.
