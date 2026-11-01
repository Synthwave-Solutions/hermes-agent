# Background review retains its current provider identity

A background skill/memory review and a `/btw` side-question fork use the same
live runtime as their parent unless an auxiliary model is explicitly configured.
The compatible OpenAI transport name (`custom` or `openai`) does not identify a
named custom route. Losing `custom:omniroute` when constructing that fork caused
its loopback URL to select native inference patience again: an infinite
non-stream stale timeout and the existing 900-second local streaming ceiling.

The review runtime now carries the named requested provider into the actual
AIAgent constructor. A named custom identity is retained only on its current
compatible transport. A different actual provider wins after fallback; an
explicit auxiliary runtime supplies its own requested identity instead of
inheriting the parent's. Auxiliary routing decisions, models, credentials and
fallback selection are unchanged. Older parent/runtime objects without this
field keep their existing canonical provider behavior.

A same-route OmniRoute fork therefore uses the parent's existing remote
watchdog rules, including configured named/canonical timeout overrides and the
existing context/reasoning scaling. This change introduces no new timeout,
model setting, prompt, provider probe, catalog cache or governance permission.
Same-model prompt/tool/prefill parity and detached persistence remain intact.
The confirmed skill-mutation observer and its failure/cancellation completion
semantics are unchanged.

## Verification

The new hermetic tests construct real parent and detached AIAgent objects using
a temporary Hermes home. They cover canonical custom/openai with named
OmniRoute, native providers/current fallback states, explicitly selected
auxiliaries, auxiliary resolution failure, configured timeout precedence and
legacy parents. They assert prompt/tool/reasoning/prefill parity and detached
storage. A real native streaming watchdog receives a synthetic silent transport,
closes it after an advanced watchdog clock and successfully accepts a subsequent
response with the same model and High reasoning. All HTTPX sends are denied and
checked at teardown; unrelated metadata discovery is stubbed.

On the unchanged a14 base, seven of these eleven cases fail and four pass.
The fixed source passes all eleven. This is local runtime evidence. Production
background-review completion and a naturally created/patched named inline skill
notice still require a real eligible review; these tests do not insert notices
or claim that a model chose to create a skill.
