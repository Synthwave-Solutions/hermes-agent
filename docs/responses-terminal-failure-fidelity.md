# Responses terminal failure fidelity

A Responses request may fail before producing any output. The normalizer used
to validate `output` before checking `status`, replacing the provider's error
with `Responses API returned no output items`. The existing error classifier
therefore lost the distinction between quota, context and other provider errors.

The stream collector also initialized status to `completed` for legacy EOF
recovery. A `response.failed` or `response.incomplete` frame without a nested
status retained that default. With partial text, a failed stream could then be
normalized as a successful answer.

Terminal event types now determine terminal status. Failed/cancelled responses
raise their existing provider error before successful-output validation. The
change preserves delivered partial text for the caller's existing recovery,
but does not label it successful. It does not execute tools, retry an agent
turn, change retry budgets or modify governance or prompt caching.

Validation uses real stream collection and normalization with synthetic events
under the hermetic test runner. Eighteen regression cases cover empty failures,
missing or inconsistent nested status, content filters, already delivered
partial text, pending tool calls, ordinary EOF recovery and genuine empty
completion. Sixteen fail on the parent revision and all eighteen pass after the
change. The five-file focused and neighboring suite passes 114 tests.

```sh
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh \
  tests/agent/test_codex_terminal_failure_fidelity.py \
  tests/agent/test_codex_responses_adapter.py \
  tests/agent/test_codex_responses_settle_pending_tool_calls.py \
  tests/run_agent/test_run_agent_codex_responses.py \
  tests/agent/test_codex_request_transport_diagnostics.py -j 4 -q --tb=short
```

This reproduces a local defect in provider failure handling. It does not
establish the cause of a particular production failure or the observed
50-second response time; those require matching production request evidence.
