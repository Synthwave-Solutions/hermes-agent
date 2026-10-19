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
not full browser-to-agent latency. A dead-worker lifecycle regression patch
must be built into the router separately; a plugin reload is runtime recovery.

`omniroute-plugin-lifecycle.patch` is tested source hardening, NOT deployed in
the running router bundle. It settles IPC calls immediately after worker exit,
disconnect or send failure. It preserves existing fail-open behavior and does
not restart the worker. Apply and rebuild OmniRoute through its normal release
process; do not claim copying TypeScript changes the running compiled bundle.
The patch includes five regression tests. The matched live loader baseline
SHA-256 is `5862800bf365f2c96e4bd453a02da39a5a1e88b2e91888b182d0b950427eb6d4`.
Isolated tests on Node 22 and 26 passed; original dead/disconnected worker
cases took 10.100/10.021 seconds, patched cases took 77–91 ms including fixture
startup. The original worker stall needs separate diagnosis if it recurs.
