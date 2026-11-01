<p align="center"><img src="branding/synthpulse-icon-256.png" width="110" alt="SynthPulse"></p>

# SynthPulse Agentic Workstation — engine fork

This repository is **Synthwave Solutions'** fork of [Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent),
the open-source agent engine that powers our **SynthPulse Agentic Workstation** product.

This is a maintained product fork with source changes for governance, identity and profile
isolation, OmniRoute routing, background work, scheduled tasks, and SynthPulse UI integration.
Upstream updates must preserve these extensions and be tested together with the WebUI.
Do not replace the fork with an unmodified upstream checkout.

## How it fits together

| Repo | Role |
|---|---|
| **this repo** (`hermes-agent`) | the **engine** — the full Hermes Agent (gateway, skills, memory, desktop app under `apps/desktop`) that SynthPulse provisions |
| [`hermes-webui`](https://github.com/Synthwave-Solutions/hermes-webui) | the **SynthPulse web application** — conversations, bot administration, Scout, background-task presentation and Office integration |
| [`synthpulse-agentic-workstation`](https://github.com/Synthwave-Solutions/synthpulse-agentic-workstation) | the **deploy template** — env-driven Raspberry Pi kit (systemd units, backup, health, dreaming, self-healing) that stands the engine up at a client |
| [`hermes-desktop`](https://github.com/Synthwave-Solutions/hermes-desktop) | the **branded desktop app** — SynthPulse-themed standalone build of `apps/desktop` |

## v0.22.5 integration

The `integrate/hermes-v0.22.5` branch replays the SynthPulse feature tree onto the upstream
`v0.22.5` release. A history-only merge retains the original fork commits, so integrating it
does not require rewriting shared `main` history or force-pushing.

Compatibility changes retain actor/bot permissions, cron owner policy propagation,
person-specific memory and sessions, asynchronous delegation, router metadata caching,
parallel tool execution, and metadata-only observability. New upstream module boundaries
are used for provider routing, response normalization, cron bookkeeping and platform discovery.
The bundled Langfuse integration remains available instead of being removed by upstream's
plugin-catalog migration.

The paired WebUI branch is `compat/hermes-v0.22.5`; its CI pins the engine revision. Use the
package manager's locked environment, and restart both long-lived processes when deploying
the pair. Back up persistent state and drain active work before the runtime switch.

Validation covers the fork regression suite, the API server, the built-in dashboard build,
and WebUI Scout, bot delegation, scheduled-task authorization and provider compatibility.
An isolated smoke test loads the real engine through the WebUI and completes a streamed
turn against a local model stub. These checks do not prove live-provider performance.
The integration PRs record exact results and remaining lint or inherited test failures;
the existence of these branches does not mean they have been deployed.

SynthPulse-specific settings now resolve from the current profile's `config.yaml` through
`hermes_cli.synthpulse_settings`. Explicit values, including `false`, `0` and an empty string,
take precedence. Missing/null values retain the existing launcher's environment fallback:

| Profile setting | Legacy launcher fallback |
| --- | --- |
| `model.tiering` | `HERMES_MODEL_TIERING` |
| `opencode.binary` | `HERMES_OPENCODE_BIN` |
| `opencode.max_slots` | `HERMES_OPENCODE_MAX_SLOTS` |
| `webui.state_dir` | `HERMES_WEBUI_STATE_DIR` |
| `webui.attachment_dir` | `HERMES_WEBUI_ATTACHMENT_DIR` |
| `api_server.disable_caller_governance` | `HERMES_DISABLE_CALLER_GOVERNANCE` |

The embedding WebUI can continue to supply its shared spool/inbox paths. Without an explicit
path or legacy override, those paths follow the active profile. OpenCode job logs follow the
profile; concurrency slots remain shared by the installation. No settings are captured at import.

The health check's five documented `HX002` exceptions are process metadata, not configuration:
the inherited governed caller, the scheduler's current job/manager, and the legacy scoped-profile
attestation. Moving these into editable YAML would change the trusted source of execution identity.
Their producers, validation and fail-closed consumers remain intact; the general check stays enabled.

## Branding note

"SynthPulse Agentic Workstation" is the Synthwave product name. The engine's internal protocol,
env vars (`HERMES_*`), package names and APIs remain **Hermes** — branding is applied at the
product/packaging and UI layer, not the wire layer, so everything stays upstream-compatible.

Engine © Nous Research, MIT. SynthPulse branding/template © Synthwave Solutions, MIT.
See the upstream `README.md` below for the Hermes Agent documentation.
