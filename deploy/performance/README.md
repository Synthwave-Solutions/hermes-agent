# Measured transport performance fixes

`claudesub-empty-inventory.patch` applies to the standalone Claude subscription
DirectSDK plugin at base `8513ef8`. It avoids starting an inert MCP inventory
process when no tools are advertised. Strict MCP isolation, permission mode,
per-request admission, cancellation, and nonpersistent execution remain intact.
Apply with `git am` in that plugin checkout; restart the owning bridge only when
its health endpoint reports no active requests. Roll back by reverting the commit.
The patch includes the behavioral test. Fifteen targeted transport tests passed;
the unrelated legacy replay test needs adaptation to the current engine compressor API.

Phase measurements on the same host found approximately 132 ms lower median
pre-upstream setup in a small sample. Provider variation dominated total time;
this is not an end-to-end latency guarantee.

A separate incident caused a fixed ten-second delay: an enabled OmniRoute plugin
retained a dead IPC worker. Every request waited for the hook timeout. Reloading
the affected plugin through authenticated deactivate/activate management APIs
restored its hooks; do not disable its document guidance to hide the timeout.
Four matched streaming probes before recovery measured 12.154, 18.842, 15.012,
and 13.600 seconds to first content. After recovery they measured 2.208, 2.035,
2.031, and 2.120 seconds. Those measurements cover router-to-first-content,
not full browser-to-agent latency. The dead-worker lifecycle patch was subsequently built into the router on
2026-10-20; a plugin reload remains a separate runtime recovery action.

`omniroute-plugin-lifecycle.patch` is deployed in the running router bundle
as of 2026-10-20. It settles IPC calls immediately after worker exit,
disconnect or send failure. It preserves existing fail-open behavior and does
not restart the worker. Apply and rebuild OmniRoute through its normal release
process; do not claim copying TypeScript changes the running compiled bundle.
The patch includes five regression tests. The matched live loader baseline
SHA-256 is `5862800bf365f2c96e4bd453a02da39a5a1e88b2e91888b182d0b950427eb6d4`.
Isolated tests on Node 22 and 26 passed; original dead/disconnected worker
cases took 10.100/10.021 seconds, patched cases took 77–91 ms including fixture
startup. The original worker stall needs separate diagnosis if it recurs.

Deployment verification (2026-10-20): the image was rebuilt from the deployed
source, retaining the native SQLite and local bridge admission fixes and the
Office plugin. Image `synthpulse-router:lifecycle-20261020` has local image ID
`sha256:7b1a3f6d4125a5c22b791de485ef448fc55fefca646aa0cf3004ea86c9f0793e`.
The five lifecycle cases also passed against the actual compiled Turbopack
loader, not only the TypeScript fixture. Native SQLite and isolated startup
passed; live authenticated router and database health checks passed, and the
Office plugin was active and ready. Its guidance, immutability and duplicate
prevention checks passed. Environment, mounts and all three networks were
preserved. Two live `synthwave-auto` probes routed to `claude-sonnet-5-5` and
returned first content in 1.961 s (default) and 1.734 s (low effort). This small
sample is a smoke measurement, not a latency guarantee or an attributable
improvement over the recovered healthy-worker baseline.

The previous container is retained stopped as `omniroute-rollback-20261020`,
including its writable fixes. Code rollback should restore that container
after requests drain; do not restore an old database over newer user data.
