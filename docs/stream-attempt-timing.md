# Main chat stream timing

The own-account Batch12 browser trial completed in 23.5755 seconds. Preparation
was 2.6352 seconds and `agent_run` was 20.7113 seconds. Its first UI token arrived
at 23.2479 seconds. These observations do not identify provider queueing,
prefill, reasoning, internal request preparation, or retries as the cause.
The earlier 8.2095-second preparation gap did not recur (now 0.4774 seconds).

The existing stream diagnostic already counted chunks but logged its timing
only on retries. A successful main `chat_completions` attempt now also emits
one `stream_attempt_timing` JSON record to the existing local logger. It has
only fixed numerical/nullable fields, a closed outcome, and SHA-256 prefixes
of the session and logical request identities, captured before dispatch.
It contains no messages, reasoning text, arguments, tool names, URLs, keys,
response headers, model names or error strings. Existing retry logs are unchanged.

All offsets use a monotonic clock from this attempt's diagnostic start:

- `sdk_dispatch_ms`: first invocation of `chat.completions.create`, after the
  request client has been prepared. `sdk_requests` counts invocations visible
  here; SDK-internal or router-internal retries remain unknown.
- `response_headers_ms`: when the adapter exposes a response object. This
  is unavailable for headerless or completed-response adapters.
- `first_chunk_ms`: first parsed chunk observed by the native consumer, including
  role/usage-only chunks. Raw socket bytes or SSE pings are not counted.
- `first_reasoning_chunk_ms`: first parsed explicit reasoning content.
- `first_text_dispatch_ms`: first text passed to the existing delivery function.
  Downstream filtering may occur; this does not establish browser rendering.
- `end_ms`: when the native stream attempt returns or raises, before its outer
  stream-end callback and final client cleanup. A completed-response adapter can
  dispatch text without having emitted any chunks.

The record also carries the native logical API-call ordinal, stream-attempt
ordinal, observed chunk count and HTTP status. Missing or nonfinite observations
remain `null`; no unavailable provider metric is represented as zero. A zero
count is used only where the native counter was initialized and observed.

This is diagnostic evidence, not a speed change. The model, High reasoning,
request body, prompt/cache scope, tool definitions, authorization, retries and
cancellation remain unchanged. Other API transports are outside this small
patch. To isolate a future own-account run, correlate its session hash and
request hash with its fixed journal window; compare these offsets against
existing preparation, `agent_run`, and UI-token times. A delay between SDK
dispatch and first parsed chunk still cannot distinguish network/router queue,
prefill and unstreamed reasoning without evidence from that provider.

Verification uses native AIAgent streaming with inert clients and an explicit
HTTPX send refusal, including a teardown assertion against swallowed network
attempts. Controlled monotonic fixtures distinguish dispatch, headers, empty
role chunks, reasoning and delivered text; further cases cover retries,
cancellation, tool-only output, completed-response adapters, logging failure,
immutable request identity, unknown values and duplicate-summary suppression.
