# Provider waits behind OmniRoute

An explicitly selected `custom:omniroute` route forwards remote inference even
when its HTTP listener is on localhost. Previously that address caused Hermes to
apply native local-model prefill patience: a 900-second default stale-stream
window, a 1800-second socket read timeout, and no implicit nonstream stale limit.

Named OmniRoute requests now use the existing remote-provider timeout rules.
The ordinary base stale limits are 180 seconds for streaming and 90 seconds for
nonstreaming; existing context-size and reasoning-model adjustments still apply.
The existing watchdog closes the request-local transport and the existing retry
logic can recover on the same selected route. Cancellation remains cancellation.
This does not choose another model, lower reasoning effort, change the prompt,
grant access or introduce a new retry policy.

The current provider identity takes priority after a fallback. Initialization
fallback now updates the requested identity alongside the active provider, as
the existing runtime fallback already does. A compatible
client resolved as `custom` or `openai` retains the explicit named router through
the existing `requested_provider` field. WebUI must pass that field before
normalizing a named provider to its compatible client; an engine-only release
cannot recover an identity which the caller discarded.

Configured provider and model request/stale timeouts continue to take precedence.
The saved `providers.omniroute` key is recognized for `custom:omniroute`; an exact
`providers["custom:omniroute"]` entry takes priority if both are present. When the named route has no valid
value for a timeout field, an explicit timeout on the actual canonical
`custom` or `openai` provider/model remains the fallback for that field. Existing
environment override and context scaling behavior is retained. Native Ollama,
unknown custom endpoints and other local inference services keep their previous
patience settings.

Tests drive native AIAgent streaming and nonstreaming timeout paths using a
temporary profile and a synthetic blocking request client. A controlled clock
trips the real watchdog without waiting minutes; the transport must be aborted,
the same route can recover, and explicit model/reasoning values stay unchanged.
These are hermetic regression tests, not live provider latency measurements.
