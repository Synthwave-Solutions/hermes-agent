# Refreshing the engine fork onto upstream

This is the procedure for pulling new upstream Hermes into our engine fork and
getting the result into a published SynthPulse image. It is part of the weekly
stack refresh: the engine is one component of that train, not a standalone
release.

Read [FORK.md](FORK.md) first if you do not already know which changes are ours.

**The one thing people get wrong:** rebasing the fork and pushing it does nothing
to the client. The hub repository pins this fork by commit, and the published
image keeps the old engine until that pin moves. See
[Step 6](#step-6-re-pin-the-hub-or-nothing-you-just-did-ships).

## Ground rules

- The fork is **rebased** onto `upstream/main`, not merged. Our commits keep their
  order and their identity as a stack on one upstream base.
- A rebase rewrites our SHAs. Treat every previously recorded fork SHA as
  unreachable until proven otherwise.
- **Never push to `upstream`.** Push to `origin` (the same URL as the `synthwave`
  remote).
- If the conflicts land in the streaming or governance hot paths, **holding the
  pin one week is the correct outcome.** We have done that deliberately before,
  and it beats shipping a silently broken runtime. Record the decision instead of
  forcing the rebase.

## Step 0: measure before you touch anything

```bash
git fetch upstream
git fetch origin
BASE=$(git merge-base origin/main upstream/main)
git log --oneline "$BASE"..origin/main          # our stack
git rev-list --count "$BASE"..upstream/main     # how far upstream moved
git status --short                              # must be clean
```

If the tree is dirty, stop and deal with that first. An engine update has already
eaten our profile scope wiring once via an autostash.

## Step 1: back up the current mainline

The convention already in the repository is a dated backup branch:

```bash
git branch "backup/main-pre-refresh-$(date +%Y%m%d%H%M)" origin/main
git push origin "backup/main-pre-refresh-$(date +%Y%m%d%H%M)"
```

Also write down the current fork tip SHA and the hub's current `SP_ENGINE_REF`
before starting, so a rollback is a one-line change rather than an archaeology
session.

## Step 2: trial rebase on a scratch branch

Do the first attempt on a throwaway branch (or a separate worktree), never on
`main` directly:

```bash
git switch -c "refresh/upstream-$(date +%Y%m%d)" origin/main
git rebase upstream/main
```

Work the conflicts with the table below. After each resolution:

```bash
git add <files>
git rebase --continue
```

To abandon the attempt cleanly at any point:

```bash
git rebase --abort
```

## Step 3: the conflicts that recur

These are the ones that have come back at more than one refresh. The resolution
principle is always the same: **take the newer upstream functionality, then
re-apply our layer on top.** Never resolve a conflict by discarding upstream's
change to keep our version of a function whole.

| File | Why it conflicts | How to resolve |
|---|---|---|
| `agent/chat_completion_helpers.py` | Our OmniRoute header capture sits inside upstream's streaming and non-streaming dispatch, which upstream changes often. This is the hard one. | Take upstream's dispatch structure. Re-insert `_extract_omniroute_route()` / `capture_omniroute_route()` and keep the non-streaming path on `with_raw_response` so response headers stay reachable. Re-read the surrounding function after resolving, not just the conflict hunk. |
| `hermes_cli/banner.py` | Both sides edit the version label and the model/provider line. | Take upstream's branch logic (including any new unconfigured-model branch), keep our skin lookups for `agent_name` and `provider`, and keep the fork repository URLs. |
| `apps/desktop/package.json` | Our SynthPulse product naming plus new upstream keys (new usage descriptions, targets, versions). | Take every upstream key, then re-apply the SynthPulse names listed in FORK.md. Leave `appId`, `executableName` and the `hermes` scheme alone. |
| `web/src/i18n/*.ts` | Upstream adds keys, we rebrand values. | Keep all new upstream keys, rebrand the user-visible strings. |
| `web/src/index.css`, `web/src/themes/presets.ts`, `web/src/App.tsx` | Upstream refactors the dashboard shell and design tokens under our branding layer. | Take the refactor, re-apply the SynthPulse layer. If upstream deleted a component our branding touched, check for remaining imports (`grep -rn "components/<Name>" web/src`) and drop it rather than resurrecting it. |
| `hermes_cli/web_server.py` | Our governance middleware and `/api/governance/*` routes sit among upstream's dashboard routes, which have been reorganised (including a move to lazily registered routers). | Take upstream's registration structure, re-attach the governance middleware, and then do the route catalog pass below. |
| `hermes_cli/dashboard_governance/route_catalog.py` | Not a textual conflict: a silent one. | Every `/api/*` route upstream added must be classified here. Unclassified routes are denied as `unknown_route` in `enforce` mode. Diff upstream's route registrations against the catalog every single refresh. |
| `tools/approval.py`, `tools/file_tools.py`, `tools/terminal_tool.py` | Our profile scope checks sit at seams upstream also edits. | Take upstream's seam, re-apply the scope check. `tests/tools/test_profile_scope_wiring.py` exists to catch a silent drop. |

## Step 4: build the frontend, and check the right directory

```bash
npm install --workspace web
npm run build -w web
test -s hermes_cli/web_dist/index.html && echo "bundle present"
```

**The bundle lands in `hermes_cli/web_dist/`, not `web/dist/`.** That is the
`build.outDir` configured in `web/vite.config.ts`, and it is what the FastAPI
dashboard serves as a static SPA. A build that "succeeded" while
`hermes_cli/web_dist/index.html` is missing or empty means the dashboard has
nothing to serve: it answers with an error even when credentials are correct.
Treat a missing bundle as a failed refresh, never as a warning.

A type-only check while resolving frontend conflicts, before the full build:

```bash
cd web && npx tsc -b
```

## Step 5: test, then publish the fork

```bash
scripts/run_tests.sh tests/hermes_cli tests/agent tests/tools tests/run_agent
```

`scripts/run_tests.sh` is the canonical runner and matches CI behaviour
(per-file isolation, `TZ=UTC`, blanked environment). Plain `pytest tests/ -v`
works with the venv activated but is not the parity path.

Then fast-forward the fork mainline and push:

```bash
git push origin HEAD:main
```

Record the new tip:

```bash
git rev-parse origin/main
```

## Step 6: re-pin the hub, or nothing you just did ships

The hub repository (`synthpulse-agentic-workstation`) builds the core image by
cloning this fork and checking out an exact commit:

```dockerfile
ARG SP_ENGINE_REPO=https://github.com/Synthwave-Solutions/hermes-agent.git
ARG SP_ENGINE_REF=<commit sha>
RUN git clone "${SP_ENGINE_REPO}" ... && git checkout -q "${SP_ENGINE_REF}" ...
```

The hub's image workflow does **not** pass `SP_ENGINE_REF` as a build argument:
the default written in the hub `Dockerfile` is what gets built. So the refresh is
not finished until that default is edited and committed in the hub repository.

Two failure modes to respect:

1. **Stale layer.** The clone plus build step is one long cache layer. If the
   `SP_ENGINE_REF` string does not change, Buildx reuses the cached layer and the
   published image keeps the **old engine** while every other signal (build green,
   image pushed, new tag) says success. Bumping the pin is what busts that cache.
2. **Unreachable pin.** Because the refresh rewrote history, a pin copied from
   notes may no longer exist on the fork. A core build has already had to be
   repaired for exactly this. Verify before committing the bump:

```bash
git fetch origin
git merge-base --is-ancestor <new-sha> origin/main && echo "pin reachable on origin/main"
```

Then, in the hub repository, bump `ARG SP_ENGINE_REF` in `Dockerfile`, note the
change in its `CHANGELOG.md`, and let its image workflow rebuild and publish.
Tags produced there are `:edge` on `main` and `:X.Y.Z` plus `:latest` on a `v*`
tag. See the hub repository's `docs/DOCKER.md` for the release checklist and
`scripts/check-versions.sh` for the pin gate.

## Step 7: verify the published engine

Minimum checks after the hub image republishes:

- The dashboard serves a bundle and the page title reads SynthPulse.
- `hermes governance validate` accepts the client policy, and a `report_only` pass shows
  no unexpected would-deny events for routes upstream just added.
- A live model call still resolves its context window through OmniRoute headers.
- The terminal banner reads SynthPulse (requires `display.skin: synthpulse` in the
  engine config; see FORK.md).

## Rollback

Revert the hub's `SP_ENGINE_REF` to the previously recorded SHA and rebuild. The
engine fork itself can be restored from the dated backup branch created in Step 1.
Roll back the pin first: it is the change that reaches clients.
