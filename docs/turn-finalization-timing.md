# Measuring the delay after the last token

The existing local INFO logger now records only the beginning and successful
return of the native turn finalizer. The finalizer starts after the conversation
loop has finished processing the provider response. Comparing this boundary with
the same host's journal token timestamp distinguishes delay before finalization
from delay inside it. These checkpoints do not identify an individual provider
or cleanup operation as the cause.

`Turn finalization timing: ` records contain a fixed event (`started` or
`completed`), SHA-256 session ID, background-review flag, Unix `created_at` and,
when both clock samples are available, monotone `duration_ms`. No message,
prompt, model, tool argument, skill name, profile name or path is included.
Wall-clock comparisons assume no intervening host clock adjustment; the total
finalizer duration uses a monotone clock.

The change does not alter persistence, audit, memory ordering, ownership,
cleanup, hooks, skill review or provider settings. External-memory provider work
already runs on the existing serial worker with copied context; this observation
does not add another background queue. A failed clock or logger cannot change
the finalizer's result. An exception that escapes before return has no completed
record, and an absent record does not prove that a phase did not run.

Production acceptance requires records from an actual own session alongside its
journal and confirmed current runtime. This instrumentation is not itself a
latency improvement. Native tests cover preserved durable calls and results,
exceptions, logging/clock failures and a synthetic response through the real
provider loop and review finalizer, without external requests.
