# 02. Governance Architecture

## Doel

Bouw een governance-laag in Hermes Dashboard die exact bepaalt wat een ingelogde gebruiker mag doen.

Primaire vereisten:
- Google SSO / OIDC.
- Koppeling aan SSO-groepen/claims.
- Whitelist-first RBAC.
- Profiles per account/groep.
- Skills per account/groep/profile.
- MCP servers/tools per account/groep/profile.
- Folders/workspaces per account/groep/profile met read/write/admin scopes.
- CLI/terminal command allowlist per account/groep/profile.
- Model/provider allowlist + usage caps.
- Settings allowlist per config key.
- Admin UI om dit te beheren.
- Audit en Preview Access.

## Kernprincipe: whitelist-first

Geen ingelogde gebruiker krijgt impliciet toegang tot dashboard functionaliteit. De default is:

```yaml
dashboard:
  governance:
    mode: enforce
    default_effect: deny
```

Een user mag alleen een actie uitvoeren als minimaal één grant die actie expliciet toestaat.

## Security boundaries

### 1. Authentication boundary
Bestaande dashboard-auth providerlaag blijft verantwoordelijk voor:
- login redirect/callback;
- session cookie/tokens;
- verifying/refreshing session;
- attaching session/principal aan request.

Nieuwe governance-laag leest de verified identity en claims, maar valideert login niet zelf.

### 2. Dashboard API boundary
FastAPI routes krijgen dependencies zoals:

```python
require_dashboard_permission("config:read")
require_profile_access(profile, "profile:use")
require_endpoint_allowed(request)
```

Elke route krijgt expliciete policy mapping. Geen “auth_required means allowed”.

### 3. Profile boundary
Iedere profile-scoped route:
- resolve `profile` uit query/body/global context;
- normalize `default`/empty profile;
- check `profile in effective.allowed_profiles` of wildcard grant;
- 403 als profile niet toegestaan is.

Belangrijk: `/api/profiles` retourneert alleen profiles die de principal mag zien/gebruiken.

### 4. Tool boundary
Model-visible tool schemas worden gefilterd vóór de LLM ze ziet. Maar dat is niet genoeg.

Runtime enforcement moet in of vóór `model_tools.handle_function_call(...)` gebeuren:

```python
check_tool_allowed(principal, tool_name, args, profile, session_id)
```

Dit vangt ook:
- direct tool call via bridge `tool_call`;
- MCP tools;
- execute_code sandbox calls;
- subagent/tool dispatch regressies;
- toekomstige tool surfaces.

### 5. Argument boundary
Sommige tools zijn niet binair “aan/uit”. Per-call arguments moeten worden gevalideerd.

Voorbeelden:
- `read_file(path=...)`: path moet onder allowed read root vallen.
- `write_file(path=...)`/`patch(path=...)`: path moet onder allowed write root vallen.
- `terminal(command=..., workdir=...)`: workdir moet onder allowed root vallen; command moet matchen op CLI allowlist.
- `mcp_<server>_<tool>`: server en tool moeten toegestaan zijn, en optioneel tool-argument constraints.
- `mcp_tool` sampling: model/provider in sampling requests moet apart toegestaan zijn.

### 6. Model boundary
Modelkeuze wordt gecheckt op:
- provider allowlist;
- model allowlist;
- max context/output limit;
- allowed auxiliary slots;
- allowed model capabilities (tools, vision, reasoning);
- usage caps.

Checks nodig bij:
- `/api/model/options`: filter zichtbaar aanbod;
- `/api/model/set`: block ongeautoriseerde modelwijziging;
- agent run start: block als profile config een model gebruikt dat principal niet mag;
- MCP sampling model requests.

### 7. Usage boundary
Usage caps zijn geen UI-feature maar enforcement:
- per user/group/profile caps;
- daily/monthly rolling windows;
- hard stop op overschrijding;
- soft warnings bij 80/90/100%;
- audit events.

Metrics:
- LLM input/output tokens;
- estimated spend;
- tool calls;
- terminal calls/seconds/background processes;
- file writes/patches;
- MCP calls;
- cron creates/updates;
- model changes;
- config/env writes.

## Nieuwe modules

Aanbevolen package:

```text
hermes_cli/dashboard_governance/
  __init__.py
  models.py             # dataclasses / Pydantic models
  loader.py             # read config/file, validate, cache
  resolver.py           # session/token principal -> effective access
  enforcer.py           # require/check functions
  audit.py              # append-only audit events
  usage.py              # counters + caps
  settings_catalog.py   # granular config/env/settings registry
  route_catalog.py      # endpoint -> permission mapping
  cli_policy.py         # terminal command parser/allowlist matcher
  path_policy.py        # path roots, symlink-safe checks
  mcp_policy.py         # mcp server/tool matching
  model_policy.py       # provider/model filtering/checks
```

## Principal resolution

Inputs:
- `DashboardAuthProvider.Session`: `email`, `display_name`, `provider`, `org_id`, `user_id`.
- Optional provider claims: roles/groups if self-hosted/OIDC provider exposes them.
- `TokenPrincipal` for API/service calls.
- Local governance policy.

Resolution steps:
1. Normalize email lower-case.
2. Verify domain allowlist if configured.
3. Load SSO claims: roles/groups from session/provider-specific metadata.
4. Map SSO groups to local groups.
5. Apply direct user grants.
6. Apply local group grants.
7. Apply local role grants.
8. Build effective access object.
9. Cache for session TTL or short mtime-aware TTL.

Effective object example:

```json
{
  "subject": "yaser@synthwave.solutions",
  "provider": "google",
  "roles": ["operator"],
  "groups": ["sw-engineering"],
  "profiles": ["default", "eng-ops"],
  "routes": ["/api/status", "/api/sessions", "/api/skills"],
  "permissions": ["chat:use", "sessions:read", "skills:read"],
  "tools": ["read_file", "search_files", "terminal"],
  "mcp": {"n8n": ["list_workflows", "execute_workflow"]},
  "models": {"openrouter": ["anthropic/claude-sonnet-4"]},
  "limits": {"daily_usd": 5.0}
}
```

## Policy merge semantics

Recommended v1:
- No explicit deny rules.
- Each role/group/user has grants.
- Effective grants are unioned.
- Global `default_effect: deny` means absence of grant = denied.
- Admin/owner may have `*`, but only for bootstrap owner accounts.

Why no deny in v1:
- Avoids conflict and precedence bugs.
- Easier to reason about Preview Access.
- Whitelist-first already prevents accidental access.

## Storage

V1 simple + auditable:

```text
~/.hermes/dashboard-governance.yaml
~/.hermes/dashboard-governance.audit.jsonl
~/.hermes/dashboard-governance.usage.sqlite
```

Alternative: store under config:

```yaml
dashboard:
  governance: ...
```

Recommended: support both, but canonicalize to `~/.hermes/dashboard-governance.yaml` for large policies. `config.yaml` can contain pointer/toggles:

```yaml
dashboard:
  governance:
    enabled: true
    policy_file: ~/.hermes/dashboard-governance.yaml
```

## Admin bootstrap

Need a safe first-admin path:

```yaml
bootstrap_admins:
  - michael@synthwave.solutions
```

Rules:
- Bootstrap admin only works in local/tailnet mode after SSO verified email.
- When at least one persistent owner group/user exists, UI should warn if bootstrap admins remain enabled.
- Do not auto-create admin for any domain user.

## API additions

```text
GET    /api/auth/me                         # extend with governance summary
GET    /api/governance/me                   # effective access for current user
GET    /api/governance/policy               # admin read
PUT    /api/governance/policy               # admin write full policy
GET    /api/governance/groups               # admin list groups
POST   /api/governance/groups               # admin create/update group
GET    /api/governance/users                # admin list known users
GET    /api/governance/preview/user/{id}    # admin preview effective access
GET    /api/governance/preview/group/{id}   # admin preview group grants
GET    /api/governance/audit                # admin audit feed
GET    /api/governance/usage                # admin usage/caps dashboard
POST   /api/governance/sso/sync-test        # admin test mapping for claims payload
```

## Frontend additions

- Add `GovernancePage` under `/governance`.
- Add nav item only when `governance:admin` is allowed.
- Add `GovernanceProvider` that loads `/api/governance/me` and exposes `can(permission)`, `canRoute(route)`, `allowedProfiles`, `allowedModels`, etc.
- Filter sidebar nav by route/permission.
- Filter ProfileSwitcher options.
- Filter model picker providers/models.
- Filter skills/MCP/toolsets/settings pages.
- Add 403 screen with clear explanation and request-access CTA.

## Audit events

JSONL shape:

```json
{
  "ts": "2026-...",
  "actor": "yaser@synthwave.solutions",
  "provider": "google",
  "profile": "eng-ops",
  "action": "tool.call",
  "resource": "terminal",
  "decision": "deny",
  "reason": "cli_not_whitelisted",
  "metadata": {"command_preview": "sudo systemctl restart ..."}
}
```

Audit these:
- login success/fail;
- SSO group sync changes;
- policy changes;
- route denies;
- profile denies;
- model denies;
- tool denies;
- terminal command denied/approved;
- file access denied;
- usage cap warning/exceeded;
- bootstrap admin usage.
