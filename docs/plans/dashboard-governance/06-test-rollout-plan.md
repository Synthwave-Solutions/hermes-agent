# 06. Test and Rollout Plan

## Test strategy

### Unit tests

Policy loader:
- missing file;
- malformed YAML;
- disabled/report-only/enforce modes;
- wildcard expansion;
- strict SSO group sync;
- domain allowlist.

Resolver:
- unknown user denied;
- bootstrap admin allowed;
- role grants;
- group grants;
- direct grants;
- union semantics;
- preview explanation includes grant source.

Route enforcement:
- allowed route returns 200;
- forbidden route returns 403;
- forbidden profile returns 403;
- `/api/profiles` filters profiles;
- `/api/auth/me` includes governance summary.

Tool enforcement:
- disallowed built-in tool blocked;
- disallowed MCP tool blocked;
- tool_search/tool_call cannot bypass allowed tools;
- execute_code cannot access sandbox tools outside grants;
- audit logs blocked tool call.

File enforcement:
- allowed read root works;
- write outside root denied;
- symlink escape denied;
- denied glob denied;
- existing sensitive path guard still works.

Terminal enforcement:
- terminal disabled denied;
- command not in allowlist denied;
- allowed `git status` works;
- `force=True` does not bypass governance;
- shell operator denied when `allow_shell_operators=false`;
- workdir outside roots denied;
- dangerous command approval still fires after governance allow.

MCP enforcement:
- server allowlist required;
- tool allowlist required;
- sampling denied by default;
- MCP config endpoints admin-only.

Models/caps:
- model options filtered;
- unauthorized model set denied;
- profile with forbidden model blocks run start;
- daily cap exceeded denies new model calls;
- report-only logs would-deny but allows.

Admin UI/API:
- non-admin cannot access `/api/governance/policy`;
- admin can load/save policy;
- policy validation errors are shown;
- preview access works for user/group;
- audit filters work.

### Integration tests

Scenarios:
1. Owner Google login -> sees all profiles/routes/admin tabs.
2. Operator Google login -> sees only allowed profiles/routes.
3. Operator direct URL `/env?profile=forbidden` -> 403.
4. Operator attempts terminal `systemctl restart` -> denied by governance before approval.
5. Operator attempts `read_file` outside root -> denied.
6. Operator attempts MCP tool not in allowlist -> denied.
7. Viewer sees Sessions/Analytics only and cannot start chat/tool run.
8. Report-only mode logs denied actions but does not block.

### Frontend checks

Commands:
```bash
cd /home/synthwavehq/.hermes/hermes-agent/web
npm test -- --runInBand   # if test suite exists
npm run build
```

Manual UI checks:
- nav item filtering;
- profile switcher filtering;
- model picker filtering;
- governance admin tab visibility;
- preview access modal;
- 403 screen.

### Security regression checks

Must verify:
- No secret values returned by governance APIs.
- Raw SSO tokens/claims not exposed to frontend except safe claims/groups.
- Forbidden profiles cannot be discovered via `/api/profiles` or error messages.
- Tool Search bridge cannot call hidden tools.
- MCP stdio env filtering remains intact.
- Terminal `force=True` and dangerous command approval cannot bypass governance.
- API tokens inherit explicit service principal scopes, not full owner rights.

## Rollout plan

### Stage 1: Build in `report_only`

Policy:
```yaml
mode: report_only
default_effect: deny
```

Expected:
- Existing admin flow continues working.
- Audit logs show would-deny decisions.
- Admin can tune policy without blocking work.

Duration: at least one normal workday or enough representative dashboard usage.

### Stage 2: Enforce for non-owner users

Policy:
```yaml
mode: enforce
```

Keep owner wildcard for bootstrap.

Verify:
- operator can complete expected tasks;
- viewer cannot trigger tools/config/env;
- audit denials are understandable.

### Stage 3: Remove broad owner wildcard where possible

Create smaller admin groups:
- `governance-admins`
- `profile-admins`
- `model-admins`
- `mcp-admins`
- `finance-sensitive`
- `support-operators`

### Stage 4: Add usage caps

Start warning-only thresholds. Then enforce hard caps for non-owner groups.

## Operational runbook

### Emergency lockout recovery

Because policy could lock admins out, keep one local recovery path:

```bash
mv ~/.hermes/dashboard-governance.yaml ~/.hermes/dashboard-governance.yaml.disabled
hermes gateway restart   # from shell outside active gateway/dashboard process
```

Or set:

```yaml
mode: off
```

Only local shell owner should be able to do this. Do not expose via non-admin dashboard.

### Safe config editing

Use Admin UI or CLI validation before saving:

```bash
hermes dashboard governance validate ~/.hermes/dashboard-governance.yaml
```

### Audit review cadence

Weekly:
- top denied actions;
- usage cap warnings;
- users with broad `*` grants;
- MCP servers with secrets;
- terminal command allowlist changes;
- policy changes.

## Acceptance criteria

Release is done when:
- Unknown SSO user cannot access anything unless assigned grants.
- `/api/profiles` only shows allowed profiles.
- Every management endpoint has explicit route permission mapping.
- Profile query/body parameters are enforced backend-side.
- Tool dispatch blocks disallowed built-in and MCP tools.
- File roots and CLI allowlists are enforced at argument level.
- Models/providers are filtered and blocked at set/run time.
- Usage caps can warn and enforce.
- Governance admin UI can configure groups/users/resources and preview access.
- Audit logs every deny and policy change.
- Tests cover allow and deny cases.
