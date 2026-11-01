# Bot Scout and autonomy policy

These deployment packages accompany the WebUI `release/bots-scout-20261019`
release. They use the existing Hermes cron, Kanban and plugin hooks. No
runtime state, credentials, personal bot profiles or session data are included.

- `synthwave-scout/`: read-only signal collector, deduplication ledger and the
  proactive update/advice/work-proposal prompt. Includes fresh assigned open
  tasks as well as blockers, reviews, stale tasks, cron outputs and bot screens.
- `synthwave-policy/`: pre-tool-call autonomy rules and diagnostic CLIs.
  The example rules are a starting policy, not a universal security sandbox.

## Install

Run each package's `install.sh` on the intended host. The Scout installer
accepts `--all-profiles`; the policy installer accepts `--profiles all`.
Back up existing files first. Preserve custom per-bot prompts when updating:
replace only copies that still match the previous default prompt.

The installers copy files; they do not activate Scouts or enable plugins.
Enable `synthwave-policy` in the intended profiles' existing `plugins.enabled`
lists, preserving all other entries. Review `policy/autonomy.yaml`: installation
starts in `observe`; use `enforce` to require approval/block matching actions.
Restart the affected gateway processes to load plugin changes.

The WebUI Scout toggle creates or resumes one daily job per bot. Leave other
bots disabled until selected. Refresh existing Scout jobs through the same
WebUI settings endpoint after changing a default prompt, since jobs store a
prompt snapshot. New proposals remain in triage; execution requires approval.
Enable the existing Kanban toolset for intended bot platforms if they should
manage tasks. The existing `kanban.orchestrator_profile` setting selects the
coordinator; configure it for the target deployment rather than copying an
operator's personal configuration.

Personal profiles can be explicitly marked `kind: person` in the WebUI bot
metadata so they remain separate in the organigram and cannot enable Scout.

## Validation

Run each package's tests separately from its directory, using a Python
environment with pytest and PyYAML. Policy integration tests also require a
Hermes checkout (`HERMES_ENGINE_DIR`) and its runtime environment.

Verified before publication: 47 Scout tests, 253 policy tests, WebUI regression
checks, live job/chat filtering and the three pilot job prompt snapshots.
Direct mail, Notion and GitHub collection is not part of this collector.

## Source provenance

The package sources were imported unchanged from local commits:

- Scout: `f07d8f4` (proactive advice and work proposals).
- Policy: see the commit message for the full source revision.
