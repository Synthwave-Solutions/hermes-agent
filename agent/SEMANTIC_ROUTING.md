# Automatic model routing

For `synthwave-auto`, `model.routing` enables a bounded prompt classifier. It uses
the agent's existing gateway client, identity and credentials, and chooses only
from configured `candidates`. Each candidate declares its model alias, descriptive
capabilities and allowed `efforts`; there are no task-keyword rules. Other explicit
session models are unchanged.

`classifier_model` is an explicitly allowed gateway alias. `timeout` is bounded
by a shared 30-second deadline, with no SDK retries. An optional
`fallback_classifier_model` supplies one independent upstream attempt after a
provider error, timeout or invalid response. The classifier has a 1,024-token
output budget so reasoning cannot consume the entire JSON response allowance. A turn is classified once and reassessed once after
tool work begins; subsequent iterations reuse the per-agent decision. Clean user
input comes from the request-builder provenance context. Limited recent turns and
tool outcomes plus estimated full context size inform the classifier; the system
prompt and tools remain byte-stable. Failure or invalid output preserves the
original request and is logged, never replaced by a keyword heuristic.

An empty `agent.reasoning_effort` means automatic. Explicit person/session effort,
request overrides and provider-recovery disabling always win. Routing writes only
the outgoing request's model and reasoning, never the agent's configured model or
conversation history. Decisions live on `_semantic_route_decision`; logs record
selected alias, effort, classifier, attempt count and latency without prompts or free-text reasons.
Gateway response headers remain the authority for the model actually executed.

Profile `model.tiering: false` disables routing. Remove `model.routing` to return
to the original gateway default without a classifier. Only live-verified aliases
should enter the candidate catalog; a public model name does not prove that a
particular account supports it. Keep gateway key allowlists aligned with candidates.

Regression tests: `scripts/run_tests.sh tests/agent/test_model_tiering.py`.

`_semantic_routing_status` distinguishes a selected route from fallback after both
classifiers fail. Static context size is a capacity constraint, not task difficulty.

The classifier uses temperature 0.2 to bypass the deployed gateway's <=0.1
in-flight deduplication optimization. Older gateway versions hash `messages`
after Codex translation has moved prompts to `input`, incorrectly joining
unrelated concurrent prompts. Retain this bypass until a gateway version with
the complete prompt/system/tenant hash is deployed and tested concurrently.
