# 07. Route Enforcement Map

This map is the first-pass permission catalog for `hermes_cli/web_server.py`.
Every route must be mapped explicitly. Unknown routes default deny when governance is enabled.

## Public/auth routes

| Route | Methods | Permission | Notes |
|---|---:|---|---|
| `/login` | GET | public | Login page only. |
| `/auth/login` | GET/POST | public | Starts provider login. |
| `/auth/callback` | GET | public | Completes provider login. |
| `/auth/logout` | POST/GET | authenticated | Self logout. |
| `/api/auth/me` | GET | authenticated | Return safe identity + governance summary. |
| `/api/auth/ws-ticket` | POST | authenticated | Also check `chat:use` if ticket is for chat WS; otherwise generic dashboard session. |
| static assets | GET | public/auth-gated by existing dashboard mode | No sensitive dynamic data. |

## Self/current-user governance

| Route | Methods | Permission | Notes |
|---|---:|---|---|
| `/api/governance/me` | GET | authenticated | Effective access for current user. |

## Governance admin

| Route | Methods | Permission | Notes |
|---|---:|---|---|
| `/api/governance/policy` | GET | `governance:read` | Admin only. Redact secrets. |
| `/api/governance/policy` | PUT | `governance:write` | Requires reauth for sensitive changes. |
| `/api/governance/users` | GET | `governance:read` | Admin only. |
| `/api/governance/groups` | GET/POST/PUT/DELETE | `governance:write` for mutations | Admin only. |
| `/api/governance/preview/*` | GET/POST | `governance:preview` | Admin only. |
| `/api/governance/audit` | GET | `governance:audit:read` | Admin only. |
| `/api/governance/usage` | GET | `governance:usage:read` | Admin or self-scoped variant later. |
| `/api/governance/simulate` | POST | `governance:preview` | Admin only. |

## Profiles

| Route | Methods | Permission | Profile check |
|---|---:|---|---|
| `/api/profiles` | GET | `profiles:read` | Filter result to allowed profiles. |
| `/api/profiles/active` | GET | `profiles:read` | Return active only if visible, else fallback/default/null. |
| `/api/profiles/*` create/clone/delete/rename/export/import | POST/PUT/DELETE | `profiles:admin` | Target profile admin grant required. |

## Sessions/chat/files/logs/analytics

| Route | Methods | Permission | Profile check |
|---|---:|---|---|
| `/api/sessions*` | GET | `sessions:read` | profile-scoped when applicable. |
| `/api/sessions*` | POST/PUT/DELETE | `sessions:write` | profile-scoped. |
| `/api/chat*` or PTY/chat WS | WS/POST | `chat:use` | profile access required. |
| `/api/files*` | GET | `files:read` | path root check required. |
| `/api/files*` mutation/download/upload | POST/PUT/DELETE | `files:write` | path root check required. |
| `/api/logs*` | GET | `logs:read` | logs may reveal sensitive info; admin/operator only. |
| `/api/analytics*` | GET | `analytics:read` | profile-scoped. |

## Config/settings/env

| Route | Methods | Permission | Extra check |
|---|---:|---|---|
| `/api/config` | GET | `config:read` | Filter readable setting keys if needed. |
| `/api/config` | PUT | `config:write` | Per-setting write allowlist. |
| `/api/config/schema` | GET | `config:read` | Can return schema but hide sensitive settings if no read grant. |
| `/api/config/defaults` | GET | `config:read` | Safe defaults only. |
| `/api/env*` | GET | `env:read` | Redacted values only; per-key read. |
| `/api/env*` | PUT/POST/DELETE | `env:write` | Per-key write; no raw secret echo. |

## Models

| Route | Methods | Permission | Extra check |
|---|---:|---|---|
| `/api/model/info` | GET | `model:read` | profile access required. |
| `/api/model/options` | GET | `model:read` | Filter providers/models by policy. |
| `/api/model/set` | POST/PUT | `model:write` | provider/model allowlist and caps. |
| `/api/model/auxiliary*` | GET/PUT | `model:auxiliary:read/write` | Per-slot allowlist. |
| `/api/model/recommended-default` | GET | `model:read` | Filter output if not allowed. |

## Skills/tools/MCP/plugins

| Route | Methods | Permission | Extra check |
|---|---:|---|---|
| `/api/skills*` | GET | `skills:read` | Filter by skill view allowlist. |
| `/api/skills*` mutation | POST/PUT/DELETE | `skills:write` | Skill manage allowlist. |
| `/api/tools/toolsets*` | GET | `tools:read` | Filter toolsets/tools. |
| `/api/tools/toolsets*` mutation | POST/PUT | `tools:write` | Toolset allowlist/admin. |
| `/api/mcp*` | GET | `mcp:read` | Filter servers/tools. |
| `/api/mcp*` mutation/test/login | POST/PUT/DELETE | `mcp:write` / `mcp:admin` | Server config admin, OAuth careful. |
| `/api/plugins*` | GET | `plugins:read` | Plugin manifests may expose routes. |
| `/api/plugins*` mutation | POST/PUT/DELETE | `plugins:write` | Admin only. |

## Cron/webhooks/channels/pairing/gateway/system

| Route | Methods | Permission | Notes |
|---|---:|---|---|
| `/api/cron*` | GET | `cron:read` | Filter jobs by profile/owner if later supported. |
| `/api/cron*` mutation/run | POST/PUT/DELETE | `cron:write` / `cron:run` | High risk; can schedule tools. |
| `/api/webhooks*` | GET | `webhooks:read` | Admin/operator. |
| `/api/webhooks*` mutation | POST/PUT/DELETE | `webhooks:write` | Can expose inbound triggers. |
| `/api/channels*` | GET | `channels:read` | Messaging metadata. |
| `/api/channels*` mutation | POST/PUT/DELETE | `channels:write` | Admin. |
| `/api/pairing*` | GET/POST/DELETE | `pairing:admin` | Admin only. |
| `/api/gateway*` | GET | `gateway:read` | Status. |
| `/api/gateway/restart` | POST | `gateway:restart` | Do not allow from active gateway if unsafe. |
| `/api/system*` | GET | `system:read` | May leak host info. |
| `/api/system*` mutation/update | POST/PUT | `system:ops` | Owner only. |
| `/api/status` | GET | `status:read` | Safe but still authenticated under governance. |

## Unknown future routes

When governance is enabled:
- If route not in catalog: deny by default.
- Log audit event `route.unknown_denied`.
- Admin UI should show unknown denied routes so implementers can classify them.
