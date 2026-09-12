# Stop network reads at the Responses terminal frame

`run_codex_stream` collects the final response at a terminal SSE frame, then
exhausts the managed iterator so Relay can finalize it. Previously that drain
read the provider stream again. If a provider left the HTTP stream open after
`response.completed`, the already available answer waited for EOF or a network
timeout. An immediate drain error was already tolerated, but a hanging tail
still delayed completion.

A small Responses-only iterator now exposes EOF immediately after a completed,
failed or incomplete terminal frame. Relay still consumes its final event,
runs its normal finalizer and closes the SDK resource. Completed objects from
compatible providers retain their existing non-streaming fallback. The change
does not retry the request, add a timeout, execute a tool or change Relay's
logical completion and interruption policies.

Six red regression cases exercised the real `run_codex_stream` path with direct
and managed NeMo Relay execution. Their provider iterator fails immediately if
read beyond the terminal event, proving the unwanted network operation without
a timing-sensitive sleep. All six pass after the change. Two extra cases cover
closing before the first event and malformed nonterminal event types. Managed
cases verify the finalizer runs once and retains terminal status and usage.

This is an independently reproduced latency defect. It is not the established
cause of the reported 50-second production question: the matched production
measurement attributed about 29 seconds before the gateway request and about
20 seconds within that request.

```sh
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh \
  tests/agent/test_codex_terminal_stream_boundary.py \
  tests/agent/test_codex_terminal_failure_fidelity.py \
  tests/agent/test_codex_responses_adapter.py \
  tests/agent/test_codex_responses_settle_pending_tool_calls.py \
  tests/run_agent/test_run_agent_codex_responses.py \
  tests/agent/test_codex_request_transport_diagnostics.py \
  tests/agent/test_relay_llm.py -j 4 -q --tb=short
```
