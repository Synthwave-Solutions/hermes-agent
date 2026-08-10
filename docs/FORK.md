# Fork map: what is ours, what is upstream

This repository is Synthwave Solutions' fork of **[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)**.
Upstream wrote the engine. We wrote a small, well-bounded set of changes on top of it.
This document exists so nobody has to guess which is which.

Start at [SYNTHPULSE.md](../SYNTHPULSE.md) for what the fork is and how to build it.
Read [REFRESH.md](REFRESH.md) before pulling upstream.

## Attribution and licence

| | |
|---|---|
| Upstream project | Hermes Agent by Nous Research |
| Upstream repository | `https://github.com/NousResearch/hermes-agent` |
| Licence | MIT, Copyright (c) 2025 Nous Research (see `LICENSE` at the repo root, unchanged) |
| Our additions | contributed on top under the same MIT terms |

The root `LICENSE`, `README.md` body, `CONTRIBUTING.md`, `SECURITY.md` and the
translated READMEs are upstream's work. Do not edit `LICENSE`, and do not
describe the engine, its architecture or its feature set as Synthwave work in
any client-facing material. What we can honestly claim is the productisation:
the SynthPulse packaging, the governance layer, the routing integration and the
deployment story around it.

Git remotes in this checkout:

| Remote | URL | Push? |
|---|---|---|
| `origin` and `synthwave` | `https://github.com/Synthwave-Solutions/hermes-agent.git` | yes, this is our fork |
| `upstream` | `https://github.com/NousResearch/hermes-agent.git` | never |

## The fork model: rebase, not merge

Our mainline is **rebased** onto `upstream/main` at every refresh, so our commits
stay as a readable stack on top of a single upstream base commit. Two consequences
that have already cost us a release:

1. **Our commit SHAs change on every refresh.** A SHA that was valid last week is
   usually unreachable afterwards. Anything that pins a fork commit (the hub's
   `ARG SP_ENGINE_REF`) must be re-verified against the remote after a refresh,
   not copied from notes.
2. **Local checkouts go stale silently.** A working copy left at an earlier
   refresh point is not simply "a few commits behind": its history diverged. Run
   `git fetch origin && git log --oneline HEAD..origin/main` before believing
   anything you read in a local tree.

## How to regenerate the ours-versus-upstream list

Never take the table below on faith. It is a snapshot; the commands are the truth:

```bash
git fetch upstream
git fetch origin
BASE=$(git merge-base origin/main upstream/main)
git log --oneline "$BASE"..origin/main      # our commits
git diff --stat "$BASE"..origin/main        # our changed files
git rev-list --count "$BASE"..upstream/main # how far upstream has moved
```

Snapshot used for this document: fork mainline `fb70cea65`, upstream merge base
`43717123c`, 28 commits of ours across the six areas below. Files listed as
"fork mainline" exist on `origin/main`; a checkout sitting at an earlier refresh
point may not have them yet.

## Our change areas

### 1. SynthPulse terminal branding

| File | What we changed |
|---|---|
| `hermes_cli/skin_engine.py` | Adds a `synthpulse` entry to `_BUILTIN_SKINS` (Wave Blue palette `#009dff` / `#3dd3ff` / `#0a93ff`, block-art banner, Braille wave hero, `~` prompt symbol, wave-themed spinner verbs, `agent_name` and `provider` set to `SynthPulse`) and sets the module-level `_active_skin_name` default to `"synthpulse"`. |
| `hermes_cli/banner.py` | Version label and the model/provider banner line read `agent_name` and `provider` from the active skin instead of hardcoding "Hermes Agent" and "Nous Research". Update-check and release-link URLs point at the fork. |
| `branding/` | New directory: `brand.json` (Synthwave brand tokens), `synthpulse-icon-256.png`, `synthpulse-icon-1024.png`, `wavemark.svg`. |

Gotcha worth knowing: `init_skin_from_config()` in `skin_engine.py` is called by
`cli.py` at startup and falls back to `"default"` when `display.skin` is absent
from the config. The module default only wins on code paths that never call it.
To guarantee the SynthPulse skin in the interactive TUI, set `display.skin:
synthpulse` in the engine config (or `hermes skin use synthpulse`, which persists
the same key).

### 2. SynthPulse dashboard and desktop branding

| File | What we changed |
|---|---|
| `web/index.html` | Title `SynthPulse`, SynthPulse favicons and Apple touch icon, `manifest.webmanifest` link, `theme-color` `#009dff`, mobile web-app meta. |
| `web/public/` | SynthPulse icons (16/32/180/192/512), logo and symbol PNGs, `wavemark.svg`, `manifest.webmanifest`, `service-worker.js`, replaced `favicon.ico`. |
| `web/src/index.css` | SynthPulse design-system layer on top of the upstream theme variables. |
| `web/src/themes/presets.ts` | Theme labels and descriptions renamed to SynthPulse. |
| `web/src/i18n/*.ts` | User-visible strings rebranded across all shipped locales. New keys added by our governance UI live here too. |
| `web/src/App.tsx`, `web/src/components/`, `web/src/main.tsx` | Shell, typography and toast wiring for the branded UI. |
| `apps/desktop/package.json` | `productName`, `artifactName`, DMG title, shortcut and bundle display names, and the macOS usage descriptions read SynthPulse. `appId` (`com.nousresearch.hermes`), `executableName` (`Hermes`) and the `hermes` URL scheme are deliberately left as upstream. |

### 3. SynthPulse entry points

`pyproject.toml` adds three console scripts and keeps the upstream three as
aliases, so existing installs and scripts keep working:

```toml
sp-engine       = "hermes_cli.main:main"
sp-engine-agent = "run_agent:main"
sp-engine-acp   = "acp_adapter.entry:main"
hermes          = "hermes_cli.main:main"
hermes-agent    = "run_agent:main"
hermes-acp      = "acp_adapter.entry:main"
```

### 4. Dashboard governance (RBAC)

The largest area by far, and the reason this is not a clean fork. It adds
role-based access control over the dashboard and gateway API surface, plus
runtime enforcement of the resulting policy on models, tools, files and
terminal commands.

| File | What it is |
|---|---|
| `hermes_cli/dashboard_governance/` | New package: `loader.py` and `models.py` (policy parse and shape), `resolver.py` (effective access for a subject), `route_catalog.py` (route to permission map), `enforcement.py` (ASGI middleware and decision helpers), `tool_policy.py`, `model_policy.py`, `usage.py` (usage caps), `audit.py`, `context.py` (context-local propagation via `HERMES_DASHBOARD_GOVERNANCE_CONTEXT`), `cli.py` (the `governance` subcommand). |
| `hermes_cli/main.py` | Registers the `governance` subcommand (`init`, `validate`, `preview`, `sample`). |
| `hermes_cli/web_server.py` | Whitelist-first governance middleware plus the `/api/governance/*` endpoints (`me`, `effective-access`, `policy` read and write with an ETag check under a mutation lock, audit and usage reads), and model-option filtering. |
| `hermes_cli/dashboard_auth/*`, `plugins/dashboard_auth/self_hosted/` | Propagate SSO claims (roles, groups, org) into governance subjects. |
| `agent/agent_init.py` | Fail-closed model policy check at agent init. |
| `model_tools.py` | Tool-level enforcement on the dispatch path. |
| `web/src/pages/GovernancePage.tsx`, `web/src/pages/AccessDeniedPage.tsx`, `web/src/contexts/GovernanceProvider.tsx`, `web/src/contexts/governance-context.ts`, `web/src/contexts/useGovernance.ts`, `web/src/lib/api.ts` | Tabbed admin UI, permission-aware navigation and the typed client for the endpoints above. |
| `docs/dashboard-governance.md`, `docs/plans/dashboard-governance/` | Operator guide plus the design and rollout plan set. |

Read `docs/dashboard-governance.md` for the policy shape and the report-only to
enforce migration. Modes are `off`, `report_only` and `enforce`; the default
policy path is `<hermes home>/dashboard-governance.yaml`.

### 5. Profile scope enforcement (fork mainline)

Binds an active profile's scope to the tools that can act outside it, so a
governed profile cannot read or write past its boundary through a tool call.

| File | What it is |
|---|---|
| `tools/profile_scope_bridge.py` | The scope bridge tools consult. |
| `tools/approval.py`, `tools/file_tools.py`, `tools/terminal_tool.py` | Enforcement at the file, terminal and approval seams. |
| `hermes_cli/web_routers/profiles.py`, `gateway/platforms/api_server.py` | Scope-aware profile routes and API server wiring. |
| `tests/tools/test_profile_scope_wiring.py` | Regression test for the wiring. |

This wiring has been lost once already to an autostash during an engine update
and had to be restored (`tools: restore profile_scope enforcement wiring`). The
test above exists specifically so a refresh cannot drop it silently again.

### 6. OmniRoute context resolution

Our LLM router returns the provider and model it actually selected in
`x-omniroute-provider` and `x-omniroute-model` response headers. Without this
change the agent sizes its context window for the requested slug, not the served
one.

| File | What it is |
|---|---|
| `agent/chat_completion_helpers.py` | `_extract_omniroute_route()` reads the headers; `capture_omniroute_route()` resolves the routed model's context length and re-points the context compressor. The non-streaming dispatch switched to `with_raw_response` so headers are visible; the streaming path reads them off the stream response. |
| `agent/conversation_loop.py` | Calls the capture on the response path. |
| `tests/agent/test_omniroute_context_routing.py` | Coverage for both paths. |

### 7. Fork documentation

`README.md` (a short banner block at the top only), `SYNTHPULSE.md`,
`docs/FORK.md` and `docs/REFRESH.md`. Everything else in `README.md` is upstream's.

## Invariants: do not "finish" the rebranding

These are intentional, not oversights. Changing them breaks the product.

- **`HERMES_*` environment variables, the `hermes_*` Python packages and modules,
  and the `hermes` console script stay as upstream.** The hub image, the WebUI
  fork and client configuration all address the engine through those names, and
  keeping them is what makes an upstream rebase tractable.
- **`appId` `com.nousresearch.hermes`, `executableName` `Hermes` and the `hermes`
  URL scheme stay as upstream.** Changing an appId orphans installed desktop
  clients and their protocol handler registrations.
- **Branding is applied at the UI and packaging layer, never at the wire layer.**
- **Governance is fail-closed on unknown `/api/*` routes.** Any dashboard route
  upstream adds must be classified in
  `hermes_cli/dashboard_governance/route_catalog.py` or it is denied in `enforce`
  mode. This is the most common breakage after an upstream bump: budget for it.

## Tests that pin our work

Run these after any refresh, before pushing:

```bash
scripts/run_tests.sh tests/hermes_cli tests/agent tests/tools tests/run_agent
```

The fork-specific files are `tests/hermes_cli/test_dashboard_governance_*.py`,
`tests/hermes_cli/test_dashboard_auth_session_claims.py`,
`tests/agent/test_omniroute_context_routing.py`,
`tests/test_governance_tool_runtime.py`,
`tests/run_agent/test_dashboard_governance_model_runtime.py` and
`tests/tools/test_profile_scope_wiring.py`.
