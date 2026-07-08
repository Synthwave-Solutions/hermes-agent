# 04. Admin UI Specification

## Route

Add dashboard route:

```text
/governance
```

Navigation label: `Governance` with shield icon.
Visible only when current principal has `governance:admin` or `*`.

## High-level UX

The Governance page is an admin-only control surface for access policy. It should feel like OpenWebUI’s Admin Panel + Preview Access, but tailored to Hermes’ stronger local/system risks.

Tabs:
1. Overview
2. Users
3. Groups
4. Profiles
5. Resources
6. Skills
7. MCP
8. Models & Caps
9. Folders & CLI
10. Settings
11. Audit
12. Simulation / Preview Access

## 1. Overview tab

Cards:
- Governance mode: `off`, `report_only`, `enforce`.
- Default effect: always `deny` in v1.
- SSO provider status: Google/OIDC configured, allowed domains, group claim path.
- Active users count.
- Groups count.
- Denied actions last 24h.
- Usage cap warnings.
- Bootstrap admins warning.

Actions:
- Switch report-only/enforce. Requires re-auth.
- Download policy YAML.
- Upload/validate policy YAML.
- Run policy validation.

## 2. Users tab

Table columns:
- Email
- Display name
- Provider
- Last login
- SSO groups from last login
- Local groups
- Roles
- Allowed profiles summary
- Usage today/month
- Status: active/pending/blocked

Actions per user:
- View effective access.
- Assign local group override.
- Add direct grant.
- Disable user.
- Reset usage caps.
- View audit events.

Important: if `group_sync.strict=true`, UI must show that SSO-managed group membership is read-only locally.

## 3. Groups tab

Inspired by OpenWebUI, split groups into:
- Permission groups: hidden from sharing menus, only grant capabilities.
- Sharing/team groups: future-compatible for resource sharing, no permissions by default.

Fields:
- Name
- Description
- SSO group mappings
- Roles
- Direct grants
- Share visibility: `anyone`, `members`, `no_one`
- Members preview

Actions:
- Create/edit group.
- Add SSO group mappings.
- Toggle grants.
- Preview group access.

## 4. Profiles tab

For each Hermes profile:
- Profile name.
- Label/description/sensitivity.
- Which groups/users can use it.
- Which groups/users can administer it.
- Profile-scoped resources: skills, MCP, folders, model whitelist.

Actions:
- Grant profile use to group/user.
- Make profile hidden from users not allowed.
- Configure per-profile caps.
- Preview all users with access.

## 5. Resources tab

Generic resource grant editor for future extension.

Resource types:
- `profile`
- `route`
- `skill`
- `mcp_server`
- `mcp_tool`
- `model_provider`
- `model`
- `folder_root`
- `cli_command`
- `setting_key`
- `dashboard_plugin`
- `cron_job`
- `webhook`

For each resource:
- Visibility: private/restricted/admin-only/public-to-allowed-dashboard-users.
- Grants: group/user + read/use/write/admin.

## 6. Skills tab

Admin controls:
- Skill catalog with name/category/source/sensitivity.
- View allowlist per group/user/profile.
- Load allowlist per group/user/profile.
- Manage allowlist for create/patch/edit/delete/write_file/remove_file.

Policy dimensions:
- `skills.view`: can inspect skill contents.
- `skills.load`: can load into session/system prompt.
- `skills.manage`: can modify skill files.
- `skills.import`: can install external/hub skills.
- `skills.use_sensitive`: for skills that touch finance, CRM, WhatsApp, etc.

UX:
- Multi-select skills per group.
- Category presets.
- “Sensitive skill” badge.
- Show inherited grants from groups.

## 7. MCP tab

Admin controls:
- List configured MCP servers from `mcp_servers` config.
- Show server transport: stdio/http/sse/oauth.
- Show credentials exposure level: no env, explicit env keys, headers configured.
- Show discovered tools under each server.
- Toggle allowed server/tool per group/user/profile.
- Toggle MCP sampling per server/group/profile.

Policy dimensions:
- `mcp.server.use`
- `mcp.tool.call`
- `mcp.server.configure`
- `mcp.sampling.use`
- `mcp.oauth.login`

UX safeguards:
- Stdio MCP with command `npx`/`uvx` gets supply-chain warning.
- Servers with env/header secrets show “credential-bearing” badge, no values.
- Sampling default off for non-admin.
- “Test as user” button runs dry policy evaluation, not live tool call.

## 8. Models & Caps tab

Model governance sections:
- Provider allowlist.
- Model allowlist.
- Capability allowlist: tools, vision, reasoning, image/audio/video.
- Context/output caps.
- Auxiliary model slots.
- MCP sampling model allowlist.
- Usage budgets.

Caps:
- daily USD
- monthly USD
- daily tokens
- per-run tokens
- max tool calls per run
- max terminal seconds/day
- max MCP calls/day
- max background processes
- max cron jobs

UI:
- Provider/model picker filtered by existing Hermes model catalog.
- Cost estimate warnings where pricing exists.
- Report-only view: “would block” events.

## 9. Folders & CLI tab

Folder controls:
- Named root paths.
- Read/write/admin grants per root.
- Symlink escape protection indicator.
- Denied globs.
- Cross-profile path protection.

CLI controls:
- Default deny terminal.
- Allowed command templates by group/profile.
- Allowed argv0/subcommands.
- Regex pattern allowlists for advanced admins.
- Workdir roots.
- Shell operators allowed? default false.
- Network commands allowed? default false.
- Sudo allowed? default false.
- Background process allowed? default false or capped.

UX:
- Command simulation box: admin enters command + workdir + user; UI returns allow/deny and reason.
- Warning that CLI allowlists are hard to make safe if shell operators are allowed.

## 10. Settings tab

Granular config/env/settings governance.

Show settings catalog generated from Hermes `CONFIG_SCHEMA`, known env vars, and manual overrides.

Categories:
- Model settings.
- Provider credentials / keys.
- Terminal backend/settings.
- MCP server config.
- Gateway/platform config.
- Approvals/security settings.
- Memory settings.
- Skills settings.
- Cron settings.
- Dashboard themes/branding.

For each key/path:
- Sensitivity: low/medium/high/critical.
- Read grant.
- Write grant.
- Requires re-auth.
- Requires owner.
- Requires restart.

Critical settings that should default owner-only:
- `approvals.mode`
- `terminal.backend`
- `terminal.cwd`
- `mcp_servers.*`
- `.env` provider keys and tokens
- `gateway.*`
- `dashboard.auth.*`
- `plugins.*`
- `security.*`
- `privacy.*`

## 11. Audit tab

Filters:
- Actor
- Group
- Profile
- Action type
- Decision allow/deny/report-only
- Resource
- Time range

Rows:
- Timestamp
- Actor
- Action
- Resource
- Profile
- Decision
- Reason
- Metadata preview

Actions:
- Export JSONL/CSV.
- Open related policy grant.
- Add remediation note.

## 12. Simulation / Preview Access tab

Equivalent to OpenWebUI Preview Access, but for Hermes.

Inputs:
- User or group.
- Optional profile.
- Optional route/tool/model/path/command to simulate.

Outputs:
- Allowed profiles.
- Allowed routes/nav items.
- Allowed tools/toolsets.
- Allowed MCP servers/tools.
- Allowed skills view/load/manage.
- Allowed models/providers.
- Allowed folders read/write.
- Allowed CLI command templates.
- Settings read/write matrix.
- Active usage caps and current usage.
- Explanation: which role/group/user grant allowed each item.

Admin-only, read-only.

## Error/403 UX

When blocked:

Title: `Access denied`
Body:
- Action attempted.
- Resource/profile.
- Reason code.
- “Ask an administrator for access” CTA.

Do not reveal sensitive resource details to users without read access. Example: “profile not available” instead of listing forbidden profile names.
