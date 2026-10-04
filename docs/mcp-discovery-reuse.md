# MCP discovery reuse and credential-aware schema fingerprints

Two bounded corrections apply to existing MCP startup behavior:

- The persistent schema fingerprint includes resolved `headers`, `env` and `auth`, in addition to endpoint/command and tool filters. Rotating or removing these configured values invalidates the old entry. The cache stores the digest, not credential values. Existing entries miss once because the fingerprint shape changes, then normal successful discovery writes a fresh entry.
- Discovery reuses an already-live local registry without acquiring the cross-process connection-creation lock when every configured server has the exact same configuration, a live session, no connection error and no reconnect in progress. Parallel-call flags retain their existing per-config behavior. Changed, missing, parked or reconnecting servers follow the original guarded registration path. The final resolved-config security filter still runs. No new connection is created by the fast path.

The existing `lazy` opt-in, schema TTL, first-use authentication and tool governance remain unchanged. This change does not enable lazy startup, create a new shared cache, replace live-session credential refresh, or claim that out-of-band OAuth token/account changes are represented in a configuration fingerprint.

Validation uses the real temporary on-disk schema store, a real held discovery file lock, synthetic local registry state and the existing two-process cold-discovery test. Before the change, four credential-invalidation cases and the live-registry lock test failed; afterward all 12 new cases and 34 existing cache, TTL, lazy-registration and cross-process cases pass.

Production evidence preceding this change showed 11.23 seconds of MCP discovery after a restart but 50 ms in a new chat without a restart. Therefore this patch does not claim to remove the observed cold handshake cost or improve the already-fast warm measurement. It removes unnecessary lock contention and corrects cache invalidation. Fresh production verification is separate.
