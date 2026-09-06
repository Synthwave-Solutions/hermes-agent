# Startup latency: schema capability lookup

Browser tool descriptions previously resolved native image routing through the same network-enabled capability lookup used when an image is actually submitted. A cold text-only turn could therefore wait for models.dev or endpoint discovery merely to choose introductory tool wording.

Schema construction now requests cached capabilities only. Explicit configuration overrides and cached capabilities remain authoritative. On an unknown cache result, the description uses neutral guidance rather than claiming the model cannot view images. Actual image and screenshot execution still uses the existing network-enabled routing path. No tools, permissions, profile credentials, model selection, or context limits are changed.

## Evidence and limits

A production function-stack sample spent approximately eight seconds in this description lookup during a cold no-image turn. A controlled metadata-delay benchmark (500 ms injected only when network is allowed) measured the prior helper at500.4 ms and the new description path at0.287 ms; the network flags were True then False. This isolates the avoided network dependency, not a predicted whole-turn speedup.

Canonical tests cover cold schemas avoiding catalog and local endpoint probes, unchanged actual image routing, and preserved explicit vision configuration. Existing image-routing and browser execution tests remain in scope. Cold MCP connections, imports, and provider processing still have separate costs; a subsequent native cold/warm measurement is needed before claiming an overall improvement.
