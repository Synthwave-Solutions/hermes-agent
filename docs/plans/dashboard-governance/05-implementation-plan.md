# 05. Hermes WebUI Governance Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add whitelist-first, SSO-group-aware governance to the Hermes WebUI dashboard.

**Architecture:** Keep existing dashboard-auth as authentication, add a new `dashboard_governance` package for authorization, usage caps, audit and admin APIs. Enforce access in backend routes, tool dispatch, model selection and argument-level file/terminal/MCP checks. Frontend consumes effective capabilities for UX filtering and admin policy management.

**Tech Stack:** Python/FastAPI backend, existing Hermes config/profile/tool registry, React/TypeScript dashboard, YAML policy file, JSONL audit, SQLite usage counters, pytest backend tests, frontend tests/build checks.

---

## Phase 0: Baseline and safety

### Task 0.1: Create feature branch

**Objective:** isolate the governance build.

**Files:** none.

**Steps:**
1. Run:
   ```bash
   cd /home/synthwavehq/.hermes/hermes-agent
   git status --short
   git checkout -b feat/dashboard-governance-rbac
   ```
2. Expected: clean/known working tree and new branch.

### Task 0.2: Add design docs to repo docs

**Objective:** copy this plan into repo docs before code begins.

**Files:**
- Create: `docs/plans/dashboard-governance-rbac.md` or `website/docs/developer-guide/dashboard-governance-rbac.md`.

**Steps:**
1. Copy/summarize this workfolder plan into repo docs.
2. Commit:
   ```bash
   git add docs/plans/dashboard-governance-rbac.md
   git commit -m "docs: add dashboard governance implementation plan"
   ```

## Phase 1: Governance policy core

### Task 1.1: Create governance package skeleton

**Objective:** add importable package for policy and enforcement.

**Files:**
- Create: `hermes_cli/dashboard_governance/__init__.py`
- Create: `hermes_cli/dashboard_governance/models.py`
- Create: `hermes_cli/dashboard_governance/loader.py`
- Create: `tests/hermes_cli/test_dashboard_governance_loader.py`

**Test first:**
- Validate an empty/missing policy returns disabled or default deny depending config.
- Validate malformed YAML raises structured validation error.
- Validate `mode: report_only|enforce|off` accepted.

**Implementation notes:**
Use dataclasses or Pydantic if already available. Keep dependencies minimal.

Core model names:
- `GovernancePolicy`
- `GovernanceRole`
- `GovernanceGroup`
- `GovernanceUser`
- `GrantSet`
- `UsageCaps`
- `EffectiveAccess`
- `AccessDecision`

### Task 1.2: Add policy file discovery

**Objective:** resolve policy path profile-safely.

**Files:**
- Modify: `hermes_cli/dashboard_governance/loader.py`
- Test: `tests/hermes_cli/test_dashboard_governance_loader.py`

**Rules:**
1. Read `dashboard.governance.policy_file` from `config.yaml` if set.
2. Else default to `get_hermes_home() / "dashboard-governance.yaml"`.
3. Cache by mtime/size.
4. Never read secrets.
5. `mode: off` disables enforcement but can still expose admin UI warning.

### Task 1.3: Implement whitelist merge semantics

**Objective:** compute effective grants from roles/groups/users.

**Files:**
- Create: `hermes_cli/dashboard_governance/resolver.py`
- Test: `tests/hermes_cli/test_dashboard_governance_resolver.py`

**Tests:**
- Unknown user gets no grants and denied.
- Bootstrap admin gets owner grants.
- User direct grants union with group grants.
- Groups mapped from SSO claim are included.
- No deny semantics in v1.
- Wildcard `*` expands as all for checks, but preview should preserve wildcard source.

### Task 1.4: Add SSO claim mapping support

**Objective:** map OAuth/OIDC roles/groups to local policy groups.

**Files:**
- Modify: `hermes_cli/dashboard_governance/resolver.py`
- Possibly modify: dashboard auth session storage/provider to expose raw claims if not already available.
- Test: `tests/hermes_cli/test_dashboard_governance_sso_mapping.py`

**Implementation notes:**
Current `Session` dataclass does not include raw claims. Add one of:
1. `claims: dict[str, Any] = field(default_factory=dict)` to `Session` with backwards-compatible default, or
2. provider-specific side channel attached to request state.

Recommended: extend `Session` backwards-compatibly with optional `claims`.

## Phase 2: Backend route enforcement

### Task 2.1: Add route catalog

**Objective:** centralize route -> permission mapping.

**Files:**
- Create: `hermes_cli/dashboard_governance/route_catalog.py`
- Test: `tests/hermes_cli/test_dashboard_governance_routes.py`

**Initial catalog:** see `07-route-enforcement-map.md`.

Each route maps to:
- endpoint pattern;
- HTTP methods;
- permission string;
- profile scoped? yes/no;
- sensitivity;
- admin-only? yes/no.

### Task 2.2: Add FastAPI dependency helpers

**Objective:** let route handlers enforce permissions tersely.

**Files:**
- Create: `hermes_cli/dashboard_governance/enforcer.py`
- Modify: `hermes_cli/web_server.py`
- Test: `tests/hermes_cli/test_dashboard_governance_enforcer.py`

**Helper API:**

```python
def get_dashboard_subject(request: Request) -> GovernanceSubject: ...
def require_permission(permission: str): ...
def require_profile_access(profile: str | None, action: str = "use"): ...
def require_route_allowed(request: Request): ...
```

### Task 2.3: Extend `/api/auth/me`

**Objective:** frontend can see current governance capabilities.

**Files:**
- Modify: `hermes_cli/web_server.py`
- Modify: `web/src/lib/api.ts` types
- Test: backend auth me test.

**Response addition:**

```json
{
  "email": "user@example.com",
  "display_name": "User",
  "governance": {
    "mode": "enforce",
    "roles": ["operator"],
    "groups": ["sw-engineering-ops"],
    "permissions": ["sessions:read"],
    "profiles": ["default", "eng-ops"],
    "routes": ["/api/status", "/api/sessions"],
    "usage_caps": {"daily_usd": 5.0}
  }
}
```

### Task 2.4: Filter `/api/profiles`

**Objective:** users only see allowed profiles.

**Files:**
- Modify: `hermes_cli/web_server.py`
- Test: `tests/hermes_cli/test_dashboard_governance_profiles.py`

**Tests:**
- owner sees all profiles.
- operator sees only allowed profile names.
- forbidden `?profile=` on scoped endpoint returns 403.
- unknown profile still returns 404 for owner/admin.

### Task 2.5: Protect management endpoints

**Objective:** all dashboard endpoints have explicit permission checks.

**Files:**
- Modify: `hermes_cli/web_server.py`
- Tests per endpoint family.

Endpoint families:
- `/api/config`: `config:read` / `config:write`, plus setting-key checks.
- `/api/env`: `env:read` / `env:write`, plus key-level checks.
- `/api/model/*`: `model:read` / `model:write`, plus provider/model checks.
- `/api/skills`: `skills:read` / `skills:write`, plus skill-name checks.
- `/api/tools/toolsets`: `tools:read` / `tools:write`, plus toolset checks.
- `/api/mcp`: `mcp:read` / `mcp:write`, plus server/tool checks.
- `/api/cron`: `cron:read` / `cron:write` / `cron:run`.
- `/api/system`, gateway restart/update: `system:ops` or `gateway:restart`.
- `/api/webhooks`, `/api/channels`, `/api/pairing`: admin/system permissions.

## Phase 3: Tool/runtime enforcement

### Task 3.1: Add governance contextvars

**Objective:** bind current principal/profile to agent/tool execution.

**Files:**
- Create: `hermes_cli/dashboard_governance/context.py`
- Modify places where dashboard chat/PTY/API starts an agent session.
- Test: context propagation through tool call.

**Context:**
- subject email/principal;
- effective access;
- active profile;
- session id;
- request id.

### Task 3.2: Filter tool schemas by policy

**Objective:** hide unavailable tools from model prompt.

**Files:**
- Modify: `model_tools.py`
- Test: `tests/test_governance_tool_filtering.py`

**Approach:**
Add optional `governance_context` or read contextvar inside `get_tool_definitions`.
Filter by:
- allowed tool names;
- allowed toolsets;
- allowed MCP server/tool names.

Keep prompt-cache discipline: tool list must be stable for a session. If governance changes mid-session, require new session/reset.

### Task 3.3: Enforce tool calls centrally

**Objective:** block out-of-policy tool calls even if schema filtering fails.

**Files:**
- Modify: `model_tools.py` around `handle_function_call` before plugin hooks and before dispatch.
- Test: `tests/test_governance_tool_dispatch.py`

**Checks:**
- `tool_name` allowed.
- MCP tool allowed.
- Tool Search bridge underlying call allowed.
- Agent-loop tools (`memory`, `session_search`, `delegate_task`, `todo`) handled with explicit permissions.
- Audit denied calls.

### Task 3.4: Enforce file path policy

**Objective:** restrict `read_file`, `search_files`, `write_file`, `patch` by resolved path.

**Files:**
- Create: `hermes_cli/dashboard_governance/path_policy.py`
- Modify: `tools/file_tools.py` handlers or tool middleware.
- Test: `tests/tools/test_file_governance.py`

**Rules:**
- Resolve path using existing `_resolve_path_for_task` semantics.
- Resolve symlinks before allow check.
- Read requires `files:read` and root match.
- Write/patch requires `files:write` and write root match.
- Denied globs apply to both reads and writes.
- Existing sensitive path guards stay in place.

### Task 3.5: Enforce terminal/CLI policy

**Objective:** restrict terminal commands by whitelist.

**Files:**
- Create: `hermes_cli/dashboard_governance/cli_policy.py`
- Modify: `tools/terminal_tool.py` before dangerous-command approval and before env creation if possible.
- Test: `tests/tools/test_terminal_governance.py`

**Rules:**
- Terminal disabled unless `terminal:run` allowed.
- `workdir` must be under `cli.workdir_roots`.
- Shell operators disallowed by default: `;`, `&&`, `||`, `|`, `$()`, backticks, redirects, process substitution.
- Parse with `shlex` for simple commands.
- Allow if command matches explicit command spec.
- Dangerous approval is still required after governance allow; governance allow is not approval.
- `force=True` must not bypass governance.

### Task 3.6: Enforce MCP policy

**Objective:** restrict MCP calls server/tool-wise.

**Files:**
- Create: `hermes_cli/dashboard_governance/mcp_policy.py`
- Modify: `tools/mcp_tool.py` registration metadata if useful.
- Modify: central tool check in `model_tools.py`.
- Test: `tests/tools/test_mcp_governance.py`

**Rules:**
- Tool names are mapped from `mcp_{server}_{tool}` to `{server, tool}`.
- Server allowlist plus tool allowlist required.
- Sampling disabled unless explicitly allowed.
- OAuth/login/config actions admin-only.

### Task 3.7: Enforce skills policy

**Objective:** restrict skill list/view/load/manage.

**Files:**
- Modify: `tools/skills_tool.py`, `tools/skill_manager_tool.py`, skill loading path in agent prompt builder if separate.
- Test: `tests/tools/test_skills_governance.py`

**Rules:**
- `skills_list` filters to viewable skills.
- `skill_view` only returns viewable skills.
- session skill loading only loads allowed skills.
- `skill_manage` requires manage grant.
- External install/import admin-only.

## Phase 4: Model governance and usage caps

### Task 4.1: Filter model options

**Objective:** model picker only shows allowed providers/models.

**Files:**
- Create: `hermes_cli/dashboard_governance/model_policy.py`
- Modify: `/api/model/options` in `web_server.py`
- Test: `tests/hermes_cli/test_dashboard_governance_models.py`

### Task 4.2: Block unauthorized model set

**Objective:** `/api/model/set` and `/api/config` cannot select unauthorized model/provider.

**Files:**
- Modify: `web_server.py`
- Test: model set denied/allowed.

### Task 4.3: Enforce model at run start

**Objective:** user cannot use a profile that is configured with a forbidden model.

**Files:**
- Modify agent/session startup path for dashboard chat.
- Test: dashboard run with forbidden model returns 403 before LLM call.

### Task 4.4: Usage accounting

**Objective:** track and enforce caps.

**Files:**
- Create: `hermes_cli/dashboard_governance/usage.py`
- Modify model call completion hooks or existing usage analytics paths.
- Modify tool dispatch to count tool calls and terminal seconds.
- Test: `tests/hermes_cli/test_dashboard_governance_usage_caps.py`

**Counters:**
- daily/monthly tokens;
- daily/monthly USD estimate;
- tool calls;
- terminal seconds;
- MCP calls;
- file writes;
- background processes.

## Phase 5: Admin APIs

### Task 5.1: Add governance API routes

**Objective:** backend for admin UI.

**Files:**
- Modify or split from `web_server.py` into `hermes_cli/dashboard_governance/routes.py`.
- Tests: admin allowed, non-admin denied.

Routes:
```text
GET/PUT /api/governance/policy
GET     /api/governance/me
GET     /api/governance/users
GET     /api/governance/groups
POST    /api/governance/groups
GET     /api/governance/preview/user/{email}
GET     /api/governance/preview/group/{group}
GET     /api/governance/audit
GET     /api/governance/usage
POST    /api/governance/simulate
```

### Task 5.2: Add audit logging

**Objective:** append-only policy/deny/usage logs.

**Files:**
- Create: `hermes_cli/dashboard_governance/audit.py`
- Tests: redaction and JSONL shape.

## Phase 6: Frontend governance UX

### Task 6.1: Add governance API client/types

**Files:**
- Modify: `web/src/lib/api.ts`
- Create: `web/src/types/governance.ts` if project style allows.

### Task 6.2: Add GovernanceProvider

**Files:**
- Create: `web/src/contexts/GovernanceProvider.tsx`
- Create: `web/src/contexts/useGovernance.ts`
- Modify: `web/src/main.tsx` or `App.tsx` provider tree.

### Task 6.3: Filter navigation and routes

**Files:**
- Modify: `web/src/App.tsx`

Rules:
- Hide nav item if route not allowed.
- Route component still handles 403 if direct navigation occurs.
- Plugins require route/resource grant too.

### Task 6.4: Filter ProfileSwitcher

**Files:**
- Modify: `web/src/contexts/ProfileProvider.tsx`
- Modify: `web/src/components/ProfileSwitcher.tsx`

### Task 6.5: Build GovernancePage

**Files:**
- Create: `web/src/pages/GovernancePage.tsx`
- Modify i18n labels if needed.
- Add route `/governance` in `App.tsx`.

Start with MVP tabs:
1. Overview
2. Users
3. Groups
4. Profiles
5. Resources
6. Audit
7. Preview Access

Then add specialized tabs for Skills/MCP/Models/Folders/CLI/Settings.

### Task 6.6: Add 403 UX

**Files:**
- Create: `web/src/components/AccessDenied.tsx`
- Use in fetch error handling or page-level wrappers.

## Phase 7: Report-only mode and migration

### Task 7.1: Implement report-only mode

**Objective:** log would-deny decisions without blocking.

Use for safe rollout.

### Task 7.2: Add policy bootstrap command

**Objective:** generate starter policy from current config.

Possible CLI:
```bash
hermes dashboard governance init --owner michael@synthwave.solutions
hermes dashboard governance validate
hermes dashboard governance preview --user yaser@synthwave.solutions
```

Files:
- Modify: `hermes_cli/subcommands/dashboard.py` or add subcommand module.

## Phase 8: Documentation and hardening

### Task 8.1: Add user docs

**Files:**
- `website/docs/user-guide/features/dashboard-governance.md`

### Task 8.2: Add security docs

**Files:**
- `website/docs/developer-guide/dashboard-governance-security.md`

### Task 8.3: Add migration guide

Covers:
- enabling Google SSO;
- configuring SSO group claim;
- generating starter policy;
- report-only dry run;
- enforce mode;
- tailnet/reverse proxy notes.

## Minimum viable release cut

MVP should include:
1. SSO identity -> effective access resolution.
2. `/api/auth/me` governance summary.
3. `/api/profiles` filtering and profile access enforcement.
4. Route-level whitelist for all management endpoints.
5. Tool dispatch enforcement for built-in + MCP tools.
6. File root enforcement.
7. Terminal command allowlist enforcement.
8. Model provider/model allowlist on options and set.
9. Basic usage caps for daily tokens/spend/tool calls.
10. Admin Governance page with users/groups/policy YAML/preview/audit.
11. Report-only mode.
12. Tests for allow/deny cases.

## Commit strategy

Commit after each phase, and after risky sub-steps:

```bash
git commit -m "feat: add dashboard governance policy loader"
git commit -m "feat: enforce dashboard governance on profiles"
git commit -m "feat: gate tools with dashboard governance policy"
git commit -m "feat: add dashboard governance admin UI"
git commit -m "test: cover dashboard governance RBAC"
```
