# Dashboard Governance MVP

Dashboard governance adds explicit RBAC around the Hermes WebUI/API surface. It is fail-closed on unknown API routes and can run in `report_only` before enforcement.

## Quick start

```bash
hermes governance init
hermes governance validate
hermes governance preview --email admin@example.com
```

The default policy path is `~/.hermes/dashboard-governance.yaml`. Override with `dashboard.governance.policy_file` in `config.yaml` or pass `--policy` to the CLI commands.

## Modes

- `off`: legacy behavior.
- `report_only`: requests are allowed, but would-deny events are written to `~/.hermes/dashboard-governance-audit.jsonl`.
- `enforce`: denied requests return `401` or `403` and are audited.

Recommended migration:

1. Run `hermes governance init`.
2. Replace `admin@example.com` with your real dashboard admin email.
3. Keep `mode: report_only` for at least one working session.
4. Open `/governance` in the dashboard and review audit/usage.
5. Tighten roles, profiles, routes, tools, skills, MCP servers/tools, models, files, CLI commands and usage caps.
6. Switch to `mode: enforce` only after `hermes governance validate` succeeds and report-only audit has no unexpected would-deny events.

## Policy shape

Top level:

```yaml
version: 1
mode: report_only
default_effect: deny
bootstrap_admins:
  - admin@example.com
roles: {}
groups: {}
users: {}
```

Role/user grants are additive. `*` is allowed but should be reserved for administrators.

Common grant sections:

```yaml
grants:
  permissions: [sessions:read, files:read, governance:read]
  profiles: [default]
  routes: [/api/sessions*, /api/files*, /api/governance/effective-access]
  tools:
    toolsets: [web]
    builtins: [read_file]
  skills:
    view: [hermes-agent]
    load: [hermes-agent]
    manage: []
  mcp:
    servers: [github]
    tools:
      github: [list_issues, get_issue]
  models:
    providers: [openai]
    models: [gpt-5.5]
  files:
    read_roots: [/home/user/work]
    write_roots: [/home/user/work]
    denied_globs: ["**/.env", "**/*secret*"]
  cli:
    commands: [git, python, npm]
    workdir_roots: [/home/user/work]
  usage_caps:
    daily_tool_calls: 500
    monthly_tool_calls: 10000
    daily_file_writes: 100
    daily_mcp_calls: 200
    daily_background_processes: 20
```

## Security notes

- Unknown `/api/*` routes are denied in governance-enabled modes until classified in the backend route catalog.
- Backend authorization is authoritative; frontend filtering is UX only.
- Report-only audit redacts secret-like fields and stores subject hashes, not raw emails.
- MCP tools require both server and tool grants. Granting `mcp-<server>` as a toolset alone does not allow calls.
- `skill_view` and `skill_manage` require name-specific skill grants.
- File roots are checked after path normalization; denied globs apply before root allowlists.
- Terminal command allowlists check `argv0` and reject shell operators such as `&&`, `;`, pipes, redirects, command substitution and process substitution.

## Dashboard UX

- Sidebar entries are hidden when the current subject lacks the matching read/admin permission.
- Direct navigation to a hidden built-in route shows an Access Denied page.
- The global profile switcher only shows profiles in the effective profile grant set.
- `/governance` shows effective access, policy JSON, recent audit events and usage state for admins.

## Troubleshooting

- `403 Forbidden`: run `hermes governance preview --email <you>` and confirm the required permission, route and profile are granted.
- New endpoint denied as `unknown_route`: add it to `hermes_cli/dashboard_governance/route_catalog.py` with the narrowest permission.
- Policy parse errors: run `hermes governance validate --policy /path/to/file`.
- Lockout during rollout: edit the policy file locally and set `mode: report_only` or `off`, then restart the dashboard/gateway process.
