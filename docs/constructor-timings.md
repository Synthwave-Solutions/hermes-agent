# Constructor timing diagnostics

The own SynthPulse QA run on engine `ec32efec` measured 7.0241 seconds inside
`AIAgent` construction. That is a constructor total, not evidence that one
particular metadata request caused the delay. A hermetic native construction on
the same source made one model-detail request and no native-inference probes;
there was no demonstrated duplicate HTTP request to remove safely.

This change adds diagnosis only. It does not change request timeouts, metadata
precedence or freshness, context overrides, credentials, model, reasoning effort,
provider fallback, tools, plugins, memory, or operation ordering. It does not claim
that the seven-second production delay is fixed.

A WebUI constructor with an explicit lowercase hexadecimal session ID of 12 or 32
characters emits one INFO record beginning `agent_init_timing `. The JSON contains
only the validated session ID, a completed/raised outcome, and fixed stage names
with monotonic elapsed milliseconds. It contains no prompts, tool definitions,
exception messages, provider/model identifiers, filesystem paths, configuration
values, or credentials.

The non-overlapping stages are:

| Stage | Boundary |
| --- | --- |
| provider_setup | Constructor entry through provider, transport, governance, client/TLS and fallback setup |
| plugin_discovery | Profile plugin discovery and tool-registry generation capture |
| tool_definitions | The existing tool-definition selection call |
| session_setup | Tool bookkeeping, session logging and store setup before profile configuration |
| profile_setup | Profile configuration, memory and compression options through the context override |
| context_setup | Existing local-runtime load, context-engine construction and initial context resolution |
| context_hooks | Existing context-engine session-start hook and working-directory hint setup |
| finalize | Remaining accounting, optional Ollama allocation and runtime snapshot setup |

The logger is emitted after the constructor has returned or raised, so that
logging does not enter any of the measured phases. Logging itself has a small
cost; this is not a zero-overhead probe. A failed constructor records the current
phase as raised, without recording the exception text. The original exception or
cancellation propagates unchanged. Unidentified or non-WebUI construction emits
nothing. Positional internal callers that do not provide the two identifying
keyword arguments are also not observed.

ContextVar state is restored after every construction. Nested constructors get
separate traces; concurrent workers cannot change each other's current phase.
Only the session/run selected independently in the own browser should be read
from the operational log. An aggregate phase can contain several operations and
must not be presented as proof of a single network call's duration.

## Validation

Use the repository's hermetic wrapper:

```sh
HERMES_TEST_FILE_RETRIES=0 ./scripts/run_tests.sh \
  tests/agent/test_constructor_timings.py --file-retries 0 -j 2 -q
```

The native constructor regressions fail on the prior source because no phase
record exists. Controlled elapsed time in actual tool selection and model-detail
transport proves attribution to separate phases while preserving the named
router, High reasoning and live context result. Other cases cover construction
failure, cancellation, logger failure, invalid identifiers, nested construction,
concurrent isolation and rejection of unknown stage labels. All fixture HTTP is
intercepted; an unexpected request fails the test even if product code catches it.
These are local diagnostics tests, not production speed or browser acceptance.
