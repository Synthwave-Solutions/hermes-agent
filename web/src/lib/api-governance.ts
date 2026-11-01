// Governance endpoints share the authenticated, profile-scoped dashboard transport.
export function createGovernanceApi(fetchJSON: <T>(path: string, init?: RequestInit) => Promise<T>) {
  return {
  getGovernanceEffectiveAccess: async () => {
    const res = await fetchJSON<{ effective_access: GovernanceEffectiveAccessResponse }>("/api/governance/effective-access");
    return res.effective_access;
  },
  getGovernancePolicy: async () => {
    const res = await fetchJSON<{ policy: Record<string, unknown>; etag?: string }>(
      "/api/governance/policy",
    );
    return { policy: res.policy, etag: res.etag ?? null };
  },
  saveGovernancePolicy: async (policy: Record<string, unknown>, etag?: string | null) => {
    // If-Match carries the etag of the policy snapshot the editor loaded so
    // the backend can reject a full-replace that would silently revert
    // concurrent user/group edits (412 policy_conflict).
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (etag) headers["If-Match"] = etag;
    const res = await fetchJSON<{ ok: boolean; policy: Record<string, unknown>; etag?: string }>(
      "/api/governance/policy",
      {
        method: "PUT",
        headers,
        body: JSON.stringify(policy),
      },
    );
    return res;
  },
  previewGovernancePolicy: async (
    policy: Record<string, unknown>,
    subject: GovernanceSubjectInput,
  ) =>
    fetchJSON<GovernancePreviewResponse>("/api/governance/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy, subject }),
    }),
  getGovernanceAudit: (limit = 100) =>
    fetchJSON<GovernanceAuditResponse>(`/api/governance/audit?limit=${limit}`),
  getGovernanceUsage: () => fetchJSON<GovernanceUsageResponse>("/api/governance/usage"),
  getGovernanceUsers: async () => {
    const res = await fetchJSON<GovernanceUsersResponse>("/api/governance/users");
    return res.users ?? {};
  },
  putGovernanceUser: (email: string, entry: GovernanceUserEntry) =>
    fetchJSON<GovernanceUsersMutationResponse>(
      `/api/governance/users/${encodeURIComponent(email)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(entry),
      },
    ),
  deleteGovernanceUser: (email: string) =>
    fetchJSON<GovernanceUsersMutationResponse>(
      `/api/governance/users/${encodeURIComponent(email)}`,
      { method: "DELETE" },
    ),
  getGovernanceGroups: async () => {
    const res = await fetchJSON<GovernanceGroupsResponse>("/api/governance/groups");
    return res.groups ?? {};
  },
  createGovernanceGroup: (name: string, group: GovernanceGroupEntry) =>
    fetchJSON<GovernanceGroupsMutationResponse>("/api/governance/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, group }),
    }),
  putGovernanceGroup: (name: string, group: GovernanceGroupEntry) =>
    fetchJSON<GovernanceGroupsMutationResponse>(
      `/api/governance/groups/${encodeURIComponent(name)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(group),
      },
    ),
  deleteGovernanceGroup: (name: string) =>
    fetchJSON<GovernanceGroupsMutationResponse>(
      `/api/governance/groups/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  simulateGovernance: (
    policy: Record<string, unknown>,
    subject: GovernanceSubjectInput,
    request: GovernanceSimulateRequest,
  ) =>
    fetchJSON<GovernanceSimulateResponse>("/api/governance/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ policy, subject, request }),
    }),
  };
}

export interface GovernanceEffectiveAccessResponse {
  mode: "off" | "report_only" | "enforce" | string;
  subject: {
    email?: string;
    display_name?: string;
    provider?: string;
    user_id?: string;
    org_id?: string;
  };
  roles: string[];
  groups: string[];
  permissions: string[];
  profiles: string[];
  routes: string[];
  grant_sources: string[];
  is_admin: boolean;
}

export interface GovernanceAuditEvent {
  ts: string;
  event: string;
  subject_email_hash?: string;
  subject_user_id_hash?: string;
  path?: string;
  method?: string;
  reason?: string;
  mode?: string;
  report_only?: boolean;
  extra?: Record<string, unknown>;
}

export interface GovernanceAuditResponse {
  events: GovernanceAuditEvent[];
}

export interface GovernancePreviewResponse {
  ok: boolean;
  policy: Record<string, unknown>;
  effective_access: GovernanceEffectiveAccessResponse;
}

export interface GovernanceUsageResponse {
  usage: Record<string, unknown>;
}

export interface GovernanceUserEntry {
  roles?: string[];
  groups?: string[];
  grants?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface GovernanceGroupEntry {
  roles?: string[];
  sso_groups?: string[];
  description?: string;
  grants?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface GovernanceUsersResponse {
  users: Record<string, GovernanceUserEntry>;
}

export interface GovernanceGroupsResponse {
  groups: Record<string, GovernanceGroupEntry>;
}

export interface GovernanceUsersMutationResponse {
  ok: boolean;
  path: string;
  users: Record<string, GovernanceUserEntry>;
}

export interface GovernanceGroupsMutationResponse {
  ok: boolean;
  path: string;
  groups: Record<string, GovernanceGroupEntry>;
}

export interface GovernanceSubjectInput {
  email?: string;
  display_name?: string;
  provider?: string;
  user_id?: string;
  org_id?: string;
  roles?: string[];
  groups?: string[];
}

export interface GovernanceSimulateRequest {
  path: string;
  method?: string;
  profile?: string;
}

export interface GovernanceSimulateResponse {
  allowed: boolean;
  reason: string;
  required_permission: string | null;
}
