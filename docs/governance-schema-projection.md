# SynthPulse governance schema projection

Constructing a tool catalog previously repeated continuation decoding and live
workspace/bot membership checks for every tool. One constructor now expands the
immutable continuation envelopes once, checks the live scope before and after
the schema projection, and keeps the existing per-tool deny, allowlist, access
level, MCP, bot CLI and resource restrictions.

This is a local computation, not a permission cache. Its only returned values
are allowed tool names and whether the live scope was valid. Dispatch, argument
authorization and delegated work still perform their independent fresh checks.
The existing turn envelope remains a ceiling; this change does not introduce
mid-turn widening or replace execution-time policy refresh.

The existing assembled-schema cache also needs a fresh common-scope check on
each hit, including a result supplied by a concurrent constructor. It does not
re-filter synthetic bridge schemas, mutate cached schemas or retain membership
results. A denied hit updates the visible tool-name list to empty. A projection
with unavailable or revoked scope is not cached, so subsequent authorization
can recover without waiting for cache expiry. Legitimately empty tool selections
can still be cached under the existing immutable policy fingerprint.

## Verification

The new native tests exercise the actual registry, schema constructor and denied
dispatch with temporary file-backed callbacks. They cover live revocation during
projection, between constructors and before dispatch; invalid stores/callbacks;
regrant after initial denial; concurrent cache population; new policy versions;
parallel actors/profiles/roots; retained continuation ceilings; and unchanged
deny, whitelist, access level and Python/host-execution barriers.

Initial regressions: 8 failed / 14 passed against the unmodified parent. Review
added two failing denied-to-granted recovery cases. The final new file has 30
passing cases. The eight affected existing files have 257 passing tests. A new
policy-version fixture initially omitted its required whitelist role ceiling;
after correcting only that fixture, all 30 new cases passed. No baseline failures
were excluded and retries were disabled.

Use the required runner:

```sh
HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh -j 4 \
  tests/test_governance_schema_projection.py \
  tests/test_governance_tool_runtime.py \
  tests/test_governance_continuation_context.py \
  tests/test_get_tool_definitions_cache_isolation.py \
  tests/test_governance_blacklist_default_allow.py \
  tests/test_governance_interpreter_command_denies.py \
  tests/test_governance_per_user_actions.py \
  tests/test_governance_mcp_names.py \
  tests/tools/test_terminal_tool_requirements.py
```

## Measured work and remaining live gate

A separate local integration measurement uses the real WebUI
`runtime_workspace_scope` callback, real temporary ACL and governance files,
the native registry and this engine. For 40 tools and 32 workspace entries:

| Actor | ACL reads before / after | Path resolutions before / after | Local elapsed before / after |
| --- | --- | --- | --- |
| Member | 160 / 8 | 5320 / 304 | 106.8 / 6.2 ms |
| Bootstrap admin | 40 / 2 | 1360 / 106 | 30.2 / 2.2 ms |

Each side parsed the policy file once; that policy cache already existed. The
next runtime decision observed revocation immediately. No network/provider or
production operations were performed for this measurement. These synthetic
timings demonstrate removed duplicate work, not the cause or removal of the
entire previously measured production constructor delay.

After reviewed deployment, the acceptance gate is an actual process-cold own
frontend session with the same explicit OmniRoute Astra route, High reasoning,
Super agent and harmless one-tool prompt. Compare the existing preparation
markers and record first-worker provenance. Check unchanged tool availability,
the actual terminal interval, gateway rounds and frontend completion separately.
MCP retries and provider latency are separate remaining costs.
