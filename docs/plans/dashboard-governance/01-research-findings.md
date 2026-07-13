# 01. Research Findings

## Onderzochte bronnen

### Hermes codebase
Onderzocht onder `/home/synthwavehq/.hermes/hermes-agent`:

- `hermes_cli/dashboard_auth/base.py`
  - Bestaat al: `Session` met `user_id`, `email`, `display_name`, `org_id`, `provider`, tokens.
  - Bestaat al: `TokenPrincipal` met `principal`, `provider`, `scopes` voor machine/service callers.
  - Auth provider interface ondersteunt OAuth-like login, refresh, revoke, password providers en token providers.
- `plugins/dashboard_auth/self_hosted/__init__.py`
  - Bestaande self-hosted/OIDC provider kan gebruikt worden voor Google SSO.
  - Secret hoort via env, niet in config of planbestanden.
- `hermes_cli/web_server.py`
  - Centrale FastAPI app voor dashboard/API.
  - Veel endpoints zijn al `profile`-scoped via `_profile_scope(profile)` of expliciete profile parameters.
  - Belangrijke families: `/api/config`, `/api/env`, `/api/model/*`, `/api/skills`, `/api/tools/toolsets`, `/api/mcp`, `/api/profiles`, `/api/sessions`, `/api/cron`, `/api/system`, `/api/webhooks`, `/api/channels`, `/api/pairing`, `/api/gateway`, `/api/status`.
- `web/src/lib/api.ts`
  - Frontend voegt automatisch `?profile=<name>` toe aan `PROFILE_SCOPED_PREFIXES`.
  - Dit is handig als UX-mechanisme, maar mag niet de security boundary zijn.
- `web/src/contexts/ProfileProvider.tsx`
  - Eén globale management profile switcher bepaalt de profile scope voor managementpagina’s.
  - Nu worden profiles nog als globale lijst geladen; governance moet die lijst per principal filteren.
- `web/src/App.tsx`
  - Navigatie is route-array driven: makkelijk te filteren via capabilities uit `/api/auth/me` of `/api/governance/me`.
- `model_tools.py`
  - Centrale tool-schema assembly via `get_tool_definitions(...)` en centrale dispatch via `handle_function_call(...)`.
  - Tool Search bridge (`tool_search`, `tool_describe`, `tool_call`) unwrapt naar onderliggende tool en scoped nu op toolsets. Governance moet hier extra op toolnamen/resource-policy afdwingen.
  - Er is een middleware seam: `apply_tool_request_middleware`, `run_tool_execution_middleware`, `pre_tool_call`, `post_tool_call`. Goede plek voor policy/audit.
- `tools/registry.py`
  - Alle tools zitten in centrale registry met `name`, `toolset`, `schema`, `handler`, `check_fn`.
  - MCP tools registreren ook als tools; policy kan dus via dezelfde registry worden afgedwongen.
- `tools/file_tools.py`
  - File reads/writes/search/patch lossen paden relatief op tegen task/session cwd.
  - Er zijn bestaande safety guards voor devices, sensitive system paths en cross-profile soft warnings.
  - Governance moet hier argumentniveau toevoegen: read/write roots, path resolution, symlink escape checks.
- `tools/terminal_tool.py`
  - Terminal heeft dangerous command approval (`tools/approval.py`) en workdir sanitization.
  - Governance moet vóór command execution een allowlist-check doen op command, argv/program, shell operators, workdir en environment/backend.
- `tools/mcp_tool.py`
  - Native MCP client leest `mcp_servers` uit config, start stdio/HTTP servers en registreert tools met prefix `mcp_{server}_{tool}`.
  - Stdio MCP env is al gefilterd; governance moet per principal server/tool allowlist en sampling policy afdwingen.

### Hermes skill/docs context
- Dashboard moet tailnet-only blijven tenzij expliciet public exposure gevraagd wordt.
- Host-header binding is relevant wanneer dashboard achter Tailscale Serve staat.
- Dashboard is een machine-control surface, dus governance moet backend-hard zijn.

### OpenWebUI referentiepatroon
Onderzocht via officiële OpenWebUI docs:

- OpenWebUI auth/access model:
  - SSO/OIDC, LDAP, SCIM, API keys.
  - RBAC bepaalt toegang op basis van rollen en groepslidmaatschappen.
  - API keys erven rechten van de user.
- OAuth role management:
  - `OAUTH_ROLES_CLAIM`, `OAUTH_ALLOWED_ROLES`, `OAUTH_ADMIN_ROLES`.
  - Rollen worden uit OAuth claims gemapt.
- OAuth group management:
  - `OAUTH_GROUP_CLAIM` claim bevat groups.
  - Strict sync: user wordt toegevoegd/verwijderd uit groups op login volgens IdP claims.
  - Optionele JIT group creation.
- Groups in OpenWebUI:
  - Groups dienen voor permission management en resource access control.
  - OpenWebUI gebruikt additive permissions: group grants worden union-based samengevoegd.
  - Resource grants bestaan voor models, knowledge bases, tools, etc.
  - Admin UI heeft Preview Access voor user/group.

## Implicatie voor Hermes

OpenWebUI’s additive model is bruikbaar als inspiratie, maar voor Hermes is “alleen additive” onvoldoende omdat Hermes extreem krachtige local/system tools heeft. Daarom:

- Voor Hermes governance wordt **deny by default** de harde basis.
- Grants zijn whitelist-only.
- Er komen geen losse “deny” regels in v1, om conflictlogica simpel te houden.
- Effectieve access = union van direct user grants + group grants + role grants, maar alleen binnen een globale `default: deny` policy.
- Voor terminal/file/system resources geldt extra argument-level validation, niet alleen endpoint-level permissions.

## Security takeaways

1. Frontend mag alleen rechten tonen die backend al zou toestaan.
2. Route-level checks zijn nodig, maar onvoldoende: tool calls en command/file arguments moeten ook gecheckt worden.
3. MCP servers zijn supply-chain en credential boundary. Per-server en per-tool allowlists zijn verplicht.
4. Modelkeuze is governance-relevant vanwege kosten, data-exposure, provider-jurisdiction en capability differences.
5. Usage caps horen bij dezelfde governance-laag: max tokens, max spend, max terminal seconds, max tool calls, max MCP calls, max file writes.
6. Settings moeten granular zijn: niet “config write” als één knop, maar per config path/key zoals `model.*`, `terminal.backend`, `mcp_servers.*`, `approvals.mode`, `security.*`, `gateway.*`.
