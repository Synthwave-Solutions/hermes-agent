# Governance policy parsing under concurrent WebUI traffic

A live nonblocking profiler sample during slow chat switching attributed 146 of 325 collected stack samples to `load_governance_policy` through tool authorization. The sampler also reported 134 read errors, so this is directional evidence, not an exact CPU percentage. Other hotspots included sidecar metadata scanning and profile skill-tree traversal.

The 393 KiB deployment policy was parsed afresh with Python SafeLoader for every authorization. `utils.fast_safe_load` already supplies the restricted libyaml CSafeLoader with a SafeLoader fallback. Reusing it preserves per-call file reads and policy freshness rather than caching permissions or weakening authorization.

A five-run local comparison on the same private policy yielded median parse times of 452.1 ms versus 51.17 ms. Parsed structures were equal. Do not infer the same multiplier for chat-switch latency: this measures one parser under host load.

Validation: loader, enforcement and deny suites passed 33 tests, including same-size policy permission updates, invalid YAML and rejection of Python object tags. No private policy content is included here.

Activation requires affected long-running processes to reload the module. Coordinate restart with active work: persisted history does not mean running tools will resume. Verify actual session-list and chat-content latency after activation under representative load. Do not label the entire WebUI performance problem solved based on this microbenchmark alone.
