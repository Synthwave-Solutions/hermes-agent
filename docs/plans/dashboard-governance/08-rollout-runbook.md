# 08. Rollout runbook: dashboard governance from report_only to enforce

Scope: the policy file at `/home/synthwavehq/.hermes/dashboard-governance.yaml` (authored 2026-07-12, mode `report_only`), Google SSO wiring for the Hermes dashboard, and the exact flip to `enforce`. No secrets appear in this document or in the policy file.

## 0. Current state and the two-track architecture

There are TWO separate web surfaces; do not confuse them:

- **Track A (LIVE today)**: `/home/synthwavehq/hermes-webui/server.py` (the standalone hermes-webui project), running as user systemd unit `hermes-webui.service` on `127.0.0.1:8787`, exposed at the root of `https://synthwavehq.tailbdab77.ts.net/` via `tailscale serve`. A helper proxy (`hermes-webui-tailnet-proxy.service`) also binds `100.84.151.33:8787` directly. This app has its OWN auth stack (`api/auth.py` + `api/auth_oidc.py`): password auth (already enabled via `HERMES_WEBUI_PASSWORD` in `hermes-webui/.env`) plus native Google OIDC login. Its sessions are anonymous tokens: once logged in, every user has FULL access. There is no per-user RBAC here.
- **Track B (governance RBAC, built, not yet live)**: the hermes-agent dashboard (`hermes_cli/web_server.py` + `hermes_cli/dashboard_governance/`) on branch `feat/dashboard-governance-rbac` in the worktree `~/work/hermes-agent-governance-build`. This carries the full whitelist-first per-user/per-group governance (skills, MCP, CLI, models, files, usage caps), the admin UI, audit trail, and the policy file below. It enforces via middleware on `request.state` populated by the `dashboard_auth` provider layer.

Status 2026-07-12:

- Track A: Google OIDC is CONFIGURED and ACTIVE (staged in `hermes-webui/.env`, backup `.env.bak-governance-20260712`): issuer accounts.google.com, Helix client id, redirect `https://synthwavehq.tailbdab77.ts.net/api/auth/oidc/callback`, allowlist `email = michael@synthwave.solutions`. `/api/auth/status` reports `oidc_enabled: true`. Password login stays as fallback, so there is no lockout risk. The ONLY missing piece is the Google console redirect URI (step 2.2).
- Track B: policy file exists, validates, mode `report_only`, default deny, bootstrap admin `michael@synthwave.solutions`. Roles: `owner` (wildcard), `admin`, `operator`, `viewer`. Groups: `sw-admins`, `sw-engineering`, `sw-freelancers`, `sw-viewers`. All `sso_groups` values are PLACEHOLDERS (see step 3). `config.yaml` `dashboard:` block now carries `public_url`, `oauth.client_id` (id only), `oauth.portal_url`, and `governance.policy_file` (backup `~/.hermes/config.yaml.bak-governance-20260712`).
- Because hermes-webui has no per-user RBAC, the per-user governance only takes effect once Track B is deployed (either serve the hermes-agent dashboard, or port the governance middleware into hermes-webui). Until then the live UI is protected by SSO/password as a single shared gate, with the OIDC email allowlist as the only per-person control.

## 1. Validate and preview (repeat after every policy edit)

From the governance worktree (or `hermes governance ...` once the branch is merged and installed):

```bash
cd /home/synthwavehq/work/hermes-agent-governance-build
python -m hermes_cli.main governance validate --policy ~/.hermes/dashboard-governance.yaml
python -m hermes_cli.main governance preview --policy ~/.hermes/dashboard-governance.yaml \
  --email michael@synthwave.solutions --provider google
python -m hermes_cli.main governance preview --policy ~/.hermes/dashboard-governance.yaml \
  --email freelancer@example.com --provider google \
  --group hermes-freelancers@synthwave.solutions
```

Expected: `valid`, michael resolves to `owner` + `admin` with wildcard routes, the freelancer resolves to the tight `sw-freelancers` grant set only (no roles, `is_admin: false`).

## 2. Wire Google SSO

### 2.1 Reuse the existing OAuth web client

Reuse the proven Helix Google OAuth web client. Its identifiers live in `/home/synthwavehq/.config/synthwave/helix/google-oauth.env` (keys `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_PROJECT`, `GOOGLE_OAUTH_BRAND`). Never copy the secret into `config.yaml`, the policy file, or this repo; the dashboard process should read the secret from that env file (or an env var sourced from it).

### 2.2 Google Cloud console clicks (browser only, admin action)

`gcloud` on the Pi is RAPT broken, so this must be done in the browser by Michael:

1. Open Google Cloud console, select the project named in `GOOGLE_OAUTH_PROJECT` in the env file above (`synthwave-website-dev`).
2. Go to APIs and Services > Credentials, open the existing OAuth 2.0 Web application client (the one whose client id matches `GOOGLE_OAUTH_CLIENT_ID`).
3. Under Authorized redirect URIs, add BOTH (Track A live now, Track B for the governance dashboard later):
   - `https://synthwavehq.tailbdab77.ts.net/api/auth/oidc/callback`
   - `https://synthwavehq.tailbdab77.ts.net/auth/google/callback`
4. Under Authorized JavaScript origins, add: `https://synthwavehq.tailbdab77.ts.net`
5. Save. The redirect host is tailnet only; that is fine because Google OAuth only requires the browser (not Google) to reach the redirect URI.
6. Optional hardening: on the OAuth consent screen keep or set User type Internal so only `synthwave.solutions` accounts can complete the flow. The policy's `identity.allowed_domains` also pins the domain server side.

### 2.3 Hermes config keys

In `~/.hermes/config.yaml` under `dashboard:` set:

```yaml
dashboard:
  public_url: https://synthwavehq.tailbdab77.ts.net
  oauth:
    client_id: <GOOGLE_OAUTH_CLIENT_ID value>   # id only, not the secret
    portal_url: https://synthwavehq.tailbdab77.ts.net
  governance:
    policy_file: ~/.hermes/dashboard-governance.yaml
```

The client secret stays in the env file and is provided to the dashboard process environment (for example via the systemd unit `EnvironmentFile=`). The governance middleware (`hermes_cli/dashboard_governance/enforcement.py`) builds its subject from the authenticated session that the dashboard auth provider attaches to `request.state`; a Google OIDC provider registered through `hermes_cli/dashboard_auth/registry.register_provider` is the integration point.

### 2.4 Side door status

The raw tailnet proxy `hermes-webui-tailnet-proxy.py` on `100.84.151.33:8787` forwards to the same hermes-webui process, and that process now enforces auth (password + OIDC) on every non-public path regardless of transport. So the side door is no longer an unauthenticated bypass. It does run plain HTTP on the tailnet (WireGuard-encrypted between devices, but no TLS at the app layer). Recommended before `enforce`: stop and disable `hermes-webui-tailnet-proxy.service` anyway and keep only the `tailscale serve` :443 path, so cookies always travel over HTTPS.

### 2.5 hermes-webui native OIDC config reference (Track A, applied)

The live hermes-webui reads `webui_oidc` from config.yaml or these env vars (env wins). They are staged in `hermes-webui/.env` (0600, gitignored), which the systemd unit loads via `EnvironmentFile=`:

```
HERMES_WEBUI_OIDC_ISSUER=https://accounts.google.com
HERMES_WEBUI_OIDC_CLIENT_ID=<GOOGLE_OAUTH_CLIENT_ID>
HERMES_WEBUI_OIDC_CLIENT_SECRET=<from helix google-oauth.env, never in config.yaml or repos>
HERMES_WEBUI_OIDC_REDIRECT_URI=https://synthwavehq.tailbdab77.ts.net/api/auth/oidc/callback
HERMES_WEBUI_OIDC_ALLOW_CLAIM=email
HERMES_WEBUI_OIDC_ALLOW_VALUES=michael@synthwave.solutions
HERMES_WEBUI_TRUST_FORWARDED_PROTO=1
```

`ALLOW_VALUES` is comma-separated; add teammates there to admit them through Google login (they then have full UI access until Track B lands, so add people deliberately). Do NOT switch `ALLOW_CLAIM` to `hd` with value `synthwave.solutions` before Track B: that would admit every Workspace user with full access, which contradicts whitelist-first. After editing: `systemctl --user restart hermes-webui.service`.

## 3. Replace the placeholder SSO groups

Google ID tokens do not carry Workspace group membership. Group sync needs the Directory API:

1. Admin Console > Security > Access and data control > API controls > Domain-wide delegation.
2. Edit the existing `hermes-gmail-dwd` service account client (client id is in `~/.hermes/gmail-dwd-sa.json`; do not paste key material anywhere).
3. Add scope `https://www.googleapis.com/auth/admin.directory.group.readonly` to its grant. Today this scope is missing and token exchange fails with 401 unauthorized_client, which is why the policy ships with PLACEHOLDER `sso_groups` values.
4. Create or confirm the Workspace groups (suggested: `hermes-admins@`, `engineering@`, `hermes-freelancers@`, `hermes-viewers@` at `synthwave.solutions`) and replace every value marked `PLACEHOLDER` in `~/.hermes/dashboard-governance.yaml` with the real group emails.
5. Until group sync works, assign people explicitly in the `users:` block of the policy (email > roles/groups), which needs no IdP claims.

Re-run step 1 after edits.

## 4. Soak in report_only

1. Leave `mode: report_only` for at least 3 to 5 working days of normal dashboard use.
2. Watch the audit trail for would-be denials: `~/.hermes/dashboard-governance.audit.jsonl` (filter on deny decisions and `unknown_route`). Unknown `/api/*` routes fail closed under enforce, so classify any recurring unknown route in `route_catalog.py` or extend the relevant role's `routes:` list.
3. Whitelist-tune: every legitimate action that would have been denied gets an explicit grant (role, group, or user level). Do not widen with `"*"` outside the `owner` role.
4. Confirm previews for each real user before the flip (step 1 commands, one per person).

## 5. Flip to enforce

1. Edit `~/.hermes/dashboard-governance.yaml`: `mode: enforce` (single line change).
2. Validate + preview again (step 1). Validation must print `mode: enforce`.
3. Restart the dashboard process so the middleware reloads cleanly.
4. Smoke test, in this order:
   - Michael logs in via Google, sees all tabs, `/api/governance/me` shows `is_admin: true`.
   - A viewer account gets only status/sessions/analytics/logs, all writes blocked.
   - A freelancer account: chat works on the FreeLLMAPI models only, terminal accepts only `git` and `python` inside `~/work`, reads of any `.env`, `secrets/`, `*.pem`, or service account json are denied, MCP is limited to `microsoft_learn`.
   - An unknown `@synthwave.solutions` login with no group claim gets nothing beyond the self routes.
5. Confirm usage caps tick in `~/.hermes/dashboard-governance-usage.json` after a few freelancer tool calls.

## 6. Rollback

Any of these restores access instantly, no code change needed:

- Set `mode: report_only` (keeps auditing) or `mode: off` in the policy file and restart the dashboard.
- Or move the file away: `mv ~/.hermes/dashboard-governance.yaml ~/.hermes/dashboard-governance.yaml.disabled` (loader then returns an `off` policy).
- Bootstrap safety net: `michael@synthwave.solutions` is in `bootstrap_admins` and always resolves to wildcard grants, so the owner can never be locked out by a bad policy edit.

## 7. Track B deployment decision (open)

The governance branch lives in the worktree; the live repo `~/.hermes/hermes-agent` is on `main` with UNCOMMITTED changes (conversation_loop.py, api_server.py, tui_gateway/server.py, web assets) that overlap files the branch touches. Do NOT check out `feat/dashboard-governance-rbac` on the live repo before those are committed or stashed deliberately. Options, pick one with Michael:

1. Commit the live dirty changes on `main` first, then merge `feat/dashboard-governance-rbac` and serve the hermes-agent dashboard (Track B replaces or runs beside hermes-webui on another port).
2. Keep hermes-webui as the live UI and port the governance middleware + admin pages into hermes-webui (extra work, one UI).
3. Run both: hermes-webui stays on :8787 root, the governance dashboard gets its own tailscale serve path/port for admin + freelancer use.

Until decided, per-user RBAC is not active on the live UI; the OIDC email allowlist (2.5) is the access control.
