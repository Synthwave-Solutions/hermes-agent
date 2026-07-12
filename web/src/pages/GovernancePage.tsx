import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Pencil, Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { ConfirmDialog } from "@nous-research/ui/ui/components/confirm-dialog";
import { Input } from "@nous-research/ui/ui/components/input";
import { Segmented } from "@nous-research/ui/ui/components/segmented";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import {
  api,
  type GovernanceAuditEvent,
  type GovernanceEffectiveAccessResponse,
  type GovernanceGroupEntry,
  type GovernanceSimulateResponse,
  type GovernanceSubjectInput,
  type GovernanceUserEntry,
} from "@/lib/api";
import { useGovernance } from "@/contexts/useGovernance";

type GovernanceTab =
  | "overview"
  | "users"
  | "groups"
  | "resources"
  | "preview"
  | "audit"
  | "policy";

type Notify = (message: string, type: "error" | "success") => void;

const DENY_EVENTS = new Set(["deny", "would_deny"]);

const RESOURCE_KEYS = [
  "routes",
  "permissions",
  "profiles",
  "tools",
  "toolsets",
  "skills",
  "mcp",
  "models",
  "files",
  "cli",
  "settings",
  "usage_caps",
] as const;

const SIMULATE_METHODS = ["GET", "POST", "PUT", "DELETE"] as const;

function pretty(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

function compact(value: unknown): string {
  return JSON.stringify(value ?? {});
}

function describeApiError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  if (message.startsWith("403")) {
    return "Access denied: you do not have permission for this action.";
  }
  if (message.startsWith("404")) {
    return "Not found: the item or endpoint is unavailable.";
  }
  return message;
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

function countDeniedLast24h(events: GovernanceAuditEvent[]): number {
  const cutoff = Date.now() - 24 * 60 * 60 * 1000;
  return events.filter((event) => {
    if (!DENY_EVENTS.has(event.event)) return false;
    const ts = Date.parse(event.ts);
    return !Number.isFinite(ts) || ts >= cutoff;
  }).length;
}

function Section({
  title,
  actions,
  children,
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-current/15 bg-background/60 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="font-sans text-display text-sm tracking-[0.12em]">{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}

function OverviewTab({
  access,
  users,
  groups,
  usage,
  deniedLast24h,
  canAudit,
  canUsage,
}: {
  access: GovernanceEffectiveAccessResponse | null;
  users: Record<string, GovernanceUserEntry>;
  groups: Record<string, GovernanceGroupEntry>;
  usage: Record<string, unknown>;
  deniedLast24h: number;
  canAudit: boolean;
  canUsage: boolean;
}) {
  const mode = access?.mode ?? "unknown";

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Section title="Mode">
          <div className="flex items-center gap-2">
            <Badge tone={mode === "enforce" ? "success" : mode === "report_only" ? "warning" : "secondary"}>
              {mode}
            </Badge>
            {access?.is_admin && <Badge tone="outline">admin</Badge>}
          </div>
          <p className="mt-2 text-sm text-text-secondary">Default effect: deny</p>
        </Section>
        <Section title="Users">
          <p className="font-mono text-2xl">{Object.keys(users).length}</p>
          <p className="text-sm text-text-secondary">policy user entries</p>
        </Section>
        <Section title="Groups">
          <p className="font-mono text-2xl">{Object.keys(groups).length}</p>
          <p className="text-sm text-text-secondary">policy group entries</p>
        </Section>
        <Section title="Denied (24h)">
          <p className="font-mono text-2xl">{canAudit ? deniedLast24h : "n/a"}</p>
          <p className="text-sm text-text-secondary">
            {canAudit ? "deny + would-deny events" : "audit access required"}
          </p>
        </Section>
      </div>

      <Section title="My effective access">
        <div className="space-y-2 font-mono text-xs text-text-secondary">
          <div>
            subject: {access?.subject?.email || access?.subject?.user_id || "(anonymous)"}
            {access?.subject?.provider ? ` · ${access.subject.provider}` : ""}
          </div>
          <div>roles: {access?.roles?.length ? access.roles.join(", ") : "(none)"}</div>
          <div>groups: {access?.groups?.length ? access.groups.join(", ") : "(none)"}</div>
          <div>
            permissions: {access?.permissions?.length ? access.permissions.join(", ") : "(none)"}
          </div>
          <div>profiles: {access?.profiles?.length ? access.profiles.join(", ") : "(none)"}</div>
          <div>routes: {access?.routes?.length ? access.routes.join(", ") : "(none)"}</div>
          <div>
            grant sources:{" "}
            {access?.grant_sources?.length ? access.grant_sources.join(", ") : "(none)"}
          </div>
        </div>
      </Section>

      {canUsage && (
        <Section title="Usage caps">
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap font-mono text-xs text-text-secondary">
            {pretty(usage)}
          </pre>
        </Section>
      )}
    </div>
  );
}

function EntryEditor({
  idLabel,
  idValue,
  idPlaceholder,
  idEditable,
  onIdChange,
  text,
  onTextChange,
  saving,
  onSave,
  onCancel,
}: {
  idLabel: string;
  idValue: string;
  idPlaceholder: string;
  idEditable: boolean;
  onIdChange: (value: string) => void;
  text: string;
  onTextChange: (value: string) => void;
  saving: boolean;
  onSave: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="mt-4 space-y-3 rounded border border-current/15 bg-background-base p-3">
      <div className="flex flex-col gap-1">
        <span className="font-sans text-display text-xs tracking-[0.12em] text-text-secondary">
          {idLabel}
        </span>
        <Input
          disabled={!idEditable}
          onChange={(event) => onIdChange(event.target.value)}
          placeholder={idPlaceholder}
          value={idValue}
        />
      </div>
      <div className="flex flex-col gap-1">
        <span className="font-sans text-display text-xs tracking-[0.12em] text-text-secondary">
          Entry JSON (roles, groups, grants)
        </span>
        <textarea
          className="min-h-[14rem] w-full rounded border border-current/15 bg-background-base p-3 font-mono text-xs text-text-primary outline-none focus:ring-1 focus:ring-midground"
          onChange={(event) => onTextChange(event.target.value)}
          spellCheck={false}
          value={text}
        />
      </div>
      <div className="flex items-center gap-2">
        <Button disabled={saving} onClick={onSave} size="sm">
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button disabled={saving} onClick={onCancel} outlined size="sm">
          Cancel
        </Button>
      </div>
    </div>
  );
}

function UsersTab({
  users,
  canWrite,
  notify,
  onChanged,
}: {
  users: Record<string, GovernanceUserEntry>;
  canWrite: boolean;
  notify: Notify;
  onChanged: (users: Record<string, GovernanceUserEntry>) => void;
}) {
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorEmail, setEditorEmail] = useState("");
  const [editorIsNew, setEditorIsNew] = useState(true);
  const [editorText, setEditorText] = useState("{}");
  const [saving, setSaving] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const emails = useMemo(() => Object.keys(users).sort(), [users]);

  function openCreate() {
    setEditorEmail("");
    setEditorText(pretty({ roles: [], groups: [], grants: {} }));
    setEditorIsNew(true);
    setEditorOpen(true);
  }

  function openEdit(email: string) {
    setEditorEmail(email);
    setEditorText(pretty(users[email] ?? {}));
    setEditorIsNew(false);
    setEditorOpen(true);
  }

  async function save() {
    const email = editorEmail.trim();
    if (!email || !email.includes("@")) {
      notify("Enter a valid email address.", "error");
      return;
    }
    let entry: GovernanceUserEntry;
    try {
      entry = JSON.parse(editorText) as GovernanceUserEntry;
    } catch (err) {
      notify(`Invalid JSON: ${err instanceof Error ? err.message : String(err)}`, "error");
      return;
    }
    setSaving(true);
    try {
      const res = await api.putGovernanceUser(email, entry);
      onChanged(res.users ?? {});
      setEditorOpen(false);
      notify(`User ${email} saved.`, "success");
    } catch (err) {
      notify(describeApiError(err), "error");
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      const res = await api.deleteGovernanceUser(pendingDelete);
      onChanged(res.users ?? {});
      notify(`User ${pendingDelete} removed.`, "success");
    } catch (err) {
      notify(describeApiError(err), "error");
    } finally {
      setDeleting(false);
      setPendingDelete(null);
    }
  }

  return (
    <Section
      title="Users"
      actions={
        canWrite ? (
          <Button onClick={openCreate} size="sm">
            <Plus /> Add user
          </Button>
        ) : undefined
      }
    >
      {emails.length === 0 ? (
        <p className="text-sm text-text-secondary">No user entries in the policy.</p>
      ) : (
        <div className="space-y-1">
          {emails.map((email) => {
            const entry = users[email] ?? {};
            return (
              <div
                key={email}
                className="flex flex-wrap items-center justify-between gap-2 border-b border-current/10 py-2"
              >
                <div className="min-w-0 font-mono text-xs text-text-secondary">
                  <div className="text-text-primary">{email}</div>
                  <div>
                    roles: {(entry.roles ?? []).join(", ") || "(none)"} · groups:{" "}
                    {(entry.groups ?? []).join(", ") || "(none)"} · grants:{" "}
                    {Object.keys(entry.grants ?? {}).join(", ") || "(none)"}
                  </div>
                </div>
                {canWrite && (
                  <div className="flex items-center gap-2">
                    <Button onClick={() => openEdit(email)} outlined size="xs" title="Edit user">
                      <Pencil />
                    </Button>
                    <Button
                      destructive
                      onClick={() => setPendingDelete(email)}
                      outlined
                      size="xs"
                      title="Delete user"
                    >
                      <Trash2 />
                    </Button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {editorOpen && (
        <EntryEditor
          idEditable={editorIsNew}
          idLabel="Email"
          idPlaceholder="user@example.com"
          idValue={editorEmail}
          onCancel={() => setEditorOpen(false)}
          onIdChange={setEditorEmail}
          onSave={() => void save()}
          onTextChange={setEditorText}
          saving={saving}
          text={editorText}
        />
      )}

      <ConfirmDialog
        confirmLabel="Delete"
        description={`Remove ${pendingDelete ?? ""} from the governance policy? Their access falls back to group/default grants.`}
        destructive
        loading={deleting}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => void confirmDelete()}
        open={pendingDelete !== null}
        title="Delete user entry"
      />
    </Section>
  );
}

function GroupsTab({
  groups,
  canWrite,
  notify,
  onChanged,
}: {
  groups: Record<string, GovernanceGroupEntry>;
  canWrite: boolean;
  notify: Notify;
  onChanged: (groups: Record<string, GovernanceGroupEntry>) => void;
}) {
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorName, setEditorName] = useState("");
  const [editorIsNew, setEditorIsNew] = useState(true);
  const [editorText, setEditorText] = useState("{}");
  const [saving, setSaving] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const names = useMemo(() => Object.keys(groups).sort(), [groups]);

  function openCreate() {
    setEditorName("");
    setEditorText(pretty({ description: "", roles: [], sso_groups: [], grants: {} }));
    setEditorIsNew(true);
    setEditorOpen(true);
  }

  function openEdit(name: string) {
    setEditorName(name);
    setEditorText(pretty(groups[name] ?? {}));
    setEditorIsNew(false);
    setEditorOpen(true);
  }

  async function save() {
    const name = editorName.trim();
    if (!name) {
      notify("Enter a group name.", "error");
      return;
    }
    let entry: GovernanceGroupEntry;
    try {
      entry = JSON.parse(editorText) as GovernanceGroupEntry;
    } catch (err) {
      notify(`Invalid JSON: ${err instanceof Error ? err.message : String(err)}`, "error");
      return;
    }
    setSaving(true);
    try {
      const res = editorIsNew
        ? await api.createGovernanceGroup(name, entry)
        : await api.putGovernanceGroup(name, entry);
      onChanged(res.groups ?? {});
      setEditorOpen(false);
      notify(`Group ${name} saved.`, "success");
    } catch (err) {
      notify(describeApiError(err), "error");
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      const res = await api.deleteGovernanceGroup(pendingDelete);
      onChanged(res.groups ?? {});
      notify(`Group ${pendingDelete} removed.`, "success");
    } catch (err) {
      notify(describeApiError(err), "error");
    } finally {
      setDeleting(false);
      setPendingDelete(null);
    }
  }

  return (
    <Section
      title="Groups"
      actions={
        canWrite ? (
          <Button onClick={openCreate} size="sm">
            <Plus /> Add group
          </Button>
        ) : undefined
      }
    >
      {names.length === 0 ? (
        <p className="text-sm text-text-secondary">No group entries in the policy.</p>
      ) : (
        <div className="space-y-1">
          {names.map((name) => {
            const entry = groups[name] ?? {};
            return (
              <div
                key={name}
                className="flex flex-wrap items-center justify-between gap-2 border-b border-current/10 py-2"
              >
                <div className="min-w-0 font-mono text-xs text-text-secondary">
                  <div className="text-text-primary">
                    {name}
                    {entry.description ? ` · ${entry.description}` : ""}
                  </div>
                  <div>
                    roles: {(entry.roles ?? []).join(", ") || "(none)"} · sso:{" "}
                    {(entry.sso_groups ?? []).join(", ") || "(none)"} · grants:{" "}
                    {Object.keys(entry.grants ?? {}).join(", ") || "(none)"}
                  </div>
                </div>
                {canWrite && (
                  <div className="flex items-center gap-2">
                    <Button onClick={() => openEdit(name)} outlined size="xs" title="Edit group">
                      <Pencil />
                    </Button>
                    <Button
                      destructive
                      onClick={() => setPendingDelete(name)}
                      outlined
                      size="xs"
                      title="Delete group"
                    >
                      <Trash2 />
                    </Button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {editorOpen && (
        <EntryEditor
          idEditable={editorIsNew}
          idLabel="Group name"
          idPlaceholder="engineering"
          idValue={editorName}
          onCancel={() => setEditorOpen(false)}
          onIdChange={setEditorName}
          onSave={() => void save()}
          onTextChange={setEditorText}
          saving={saving}
          text={editorText}
        />
      )}

      <ConfirmDialog
        confirmLabel="Delete"
        description={`Remove group ${pendingDelete ?? ""} from the governance policy? Members lose the grants it provided.`}
        destructive
        loading={deleting}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => void confirmDelete()}
        open={pendingDelete !== null}
        title="Delete group"
      />
    </Section>
  );
}

function ResourcesTab({
  policy,
  users,
  groups,
}: {
  policy: Record<string, unknown>;
  users: Record<string, GovernanceUserEntry>;
  groups: Record<string, GovernanceGroupEntry>;
}) {
  const roles = useMemo(
    () =>
      (policy.roles && typeof policy.roles === "object"
        ? (policy.roles as Record<string, unknown>)
        : {}),
    [policy],
  );

  const resourceGrants = useMemo(() => {
    return RESOURCE_KEYS.map((key) => {
      const entries: { source: string; value: unknown }[] = [];
      for (const [name, entry] of Object.entries(groups)) {
        const grants = entry.grants ?? {};
        if (grants[key] !== undefined) {
          entries.push({ source: `group:${name}`, value: grants[key] });
        }
      }
      for (const [email, entry] of Object.entries(users)) {
        const grants = entry.grants ?? {};
        if (grants[key] !== undefined) {
          entries.push({ source: `user:${email}`, value: grants[key] });
        }
      }
      return { key, entries };
    });
  }, [users, groups]);

  const nonEmpty = resourceGrants.filter((group) => group.entries.length > 0);

  return (
    <div className="space-y-4">
      <p className="text-sm text-text-secondary">
        Read-only view of the roles and resource grants defined in the current policy. Edit them
        via the Users, Groups, or Policy tabs.
      </p>

      <Section title="Roles">
        {Object.keys(roles).length === 0 ? (
          <p className="text-sm text-text-secondary">No roles defined.</p>
        ) : (
          <div className="space-y-2">
            {Object.entries(roles).map(([name, definition]) => (
              <div key={name} className="border-b border-current/10 py-2 font-mono text-xs">
                <div className="text-text-primary">{name}</div>
                <pre className="max-h-40 overflow-auto whitespace-pre-wrap text-text-secondary">
                  {pretty(definition)}
                </pre>
              </div>
            ))}
          </div>
        )}
      </Section>

      {nonEmpty.length === 0 ? (
        <Section title="Resource grants">
          <p className="text-sm text-text-secondary">
            No resource grants (profiles, tools, skills, mcp, models, files, cli, …) are defined
            on users or groups.
          </p>
        </Section>
      ) : (
        nonEmpty.map(({ key, entries }) => (
          <Section key={key} title={key.replace("_", " ")}>
            <div className="space-y-1 font-mono text-xs text-text-secondary">
              {entries.map(({ source, value }) => (
                <div key={`${key}-${source}`} className="border-b border-current/10 py-1">
                  <span className="text-text-primary">{source}</span> → {compact(value)}
                </div>
              ))}
            </div>
          </Section>
        ))
      )}
    </div>
  );
}

function PreviewTab({
  policy,
  notify,
  defaultEmail,
}: {
  policy: Record<string, unknown>;
  notify: Notify;
  defaultEmail: string;
}) {
  const [email, setEmail] = useState(defaultEmail);
  const [rolesText, setRolesText] = useState("");
  const [groupsText, setGroupsText] = useState("");
  const [profile, setProfile] = useState("");
  const [routePath, setRoutePath] = useState("/api/sessions");
  const [method, setMethod] = useState<(typeof SIMULATE_METHODS)[number]>("GET");
  const [previewAccess, setPreviewAccess] = useState<GovernanceEffectiveAccessResponse | null>(
    null,
  );
  const [simResult, setSimResult] = useState<GovernanceSimulateResponse | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [simulating, setSimulating] = useState(false);

  function buildSubject(): GovernanceSubjectInput {
    return {
      email: email.trim(),
      roles: splitCsv(rolesText),
      groups: splitCsv(groupsText),
    };
  }

  async function runPreview() {
    setPreviewing(true);
    try {
      const res = await api.previewGovernancePolicy(policy, buildSubject());
      setPreviewAccess(res.effective_access);
    } catch (err) {
      notify(describeApiError(err), "error");
    } finally {
      setPreviewing(false);
    }
  }

  async function runSimulate() {
    setSimulating(true);
    try {
      const res = await api.simulateGovernance(policy, buildSubject(), {
        path: routePath.trim(),
        method,
        profile: profile.trim() || undefined,
      });
      setSimResult(res);
    } catch (err) {
      notify(describeApiError(err), "error");
    } finally {
      setSimulating(false);
    }
  }

  return (
    <div className="space-y-4">
      <Section title="Subject">
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <span className="font-sans text-display text-xs tracking-[0.12em] text-text-secondary">
              Email
            </span>
            <Input
              onChange={(event) => setEmail(event.target.value)}
              placeholder="user@example.com"
              value={email}
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="font-sans text-display text-xs tracking-[0.12em] text-text-secondary">
              Roles (comma separated)
            </span>
            <Input
              onChange={(event) => setRolesText(event.target.value)}
              placeholder="viewer, operator"
              value={rolesText}
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="font-sans text-display text-xs tracking-[0.12em] text-text-secondary">
              Groups (comma separated)
            </span>
            <Input
              onChange={(event) => setGroupsText(event.target.value)}
              placeholder="engineering"
              value={groupsText}
            />
          </div>
        </div>
        <div className="mt-3">
          <Button disabled={previewing} onClick={() => void runPreview()} size="sm">
            {previewing ? "Previewing…" : "Preview effective access"}
          </Button>
        </div>
        {previewAccess && (
          <div className="mt-4 space-y-1 rounded border border-current/15 bg-background-base p-3 font-mono text-xs text-text-secondary">
            <div>
              mode: {previewAccess.mode}
              {previewAccess.is_admin ? " · admin" : ""}
            </div>
            <div>roles: {previewAccess.roles.join(", ") || "(none)"}</div>
            <div>groups: {previewAccess.groups.join(", ") || "(none)"}</div>
            <div>permissions: {previewAccess.permissions.join(", ") || "(none)"}</div>
            <div>profiles: {previewAccess.profiles.join(", ") || "(none)"}</div>
            <div>routes: {previewAccess.routes.join(", ") || "(none)"}</div>
            <div>grant sources: {previewAccess.grant_sources.join(", ") || "(none)"}</div>
          </div>
        )}
      </Section>

      <Section title="Simulate a request">
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <span className="font-sans text-display text-xs tracking-[0.12em] text-text-secondary">
              Path
            </span>
            <Input
              onChange={(event) => setRoutePath(event.target.value)}
              placeholder="/api/sessions"
              value={routePath}
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="font-sans text-display text-xs tracking-[0.12em] text-text-secondary">
              Method
            </span>
            <Segmented
              onChange={setMethod}
              options={SIMULATE_METHODS.map((value) => ({ label: value, value }))}
              value={method}
            />
          </div>
          <div className="flex flex-col gap-1">
            <span className="font-sans text-display text-xs tracking-[0.12em] text-text-secondary">
              Profile (optional)
            </span>
            <Input
              onChange={(event) => setProfile(event.target.value)}
              placeholder="default"
              value={profile}
            />
          </div>
        </div>
        <div className="mt-3">
          <Button disabled={simulating} onClick={() => void runSimulate()} size="sm">
            {simulating ? "Simulating…" : "Simulate"}
          </Button>
        </div>
        {simResult && (
          <div className="mt-4 flex flex-wrap items-center gap-3 rounded border border-current/15 bg-background-base p-3">
            <Badge tone={simResult.allowed ? "success" : "destructive"}>
              {simResult.allowed ? "allowed" : "denied"}
            </Badge>
            <span className="font-mono text-xs text-text-secondary">
              reason: {simResult.reason} · required permission:{" "}
              {simResult.required_permission ?? "(none)"}
            </span>
          </div>
        )}
      </Section>
    </div>
  );
}

function AuditTab({
  events,
  refreshing,
  onRefresh,
}: {
  events: GovernanceAuditEvent[];
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const [filter, setFilter] = useState("");

  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return events;
    return events.filter((event) =>
      [event.event, event.reason, event.path, event.method, event.mode, event.ts]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [events, filter]);

  return (
    <Section
      title="Audit"
      actions={
        <Button disabled={refreshing} onClick={onRefresh} outlined size="sm">
          <RefreshCw /> {refreshing ? "Refreshing…" : "Refresh"}
        </Button>
      }
    >
      <Input
        className="mb-3 max-w-sm"
        onChange={(event) => setFilter(event.target.value)}
        placeholder="Filter by event, reason, path…"
        value={filter}
      />
      <div className="max-h-[32rem] overflow-auto font-mono text-xs text-text-secondary">
        {filtered.length === 0 ? (
          "No audit events match."
        ) : (
          filtered.map((event, index) => (
            <div
              key={`${event.ts}-${event.event}-${event.path}-${index}`}
              className="border-b border-current/10 py-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span>{event.ts}</span>
                <Badge tone={DENY_EVENTS.has(event.event) ? "destructive" : "secondary"}>
                  {event.event}
                </Badge>
                {event.reason && <span>{event.reason}</span>}
              </div>
              {(event.method || event.path) && (
                <div>
                  {event.method} {event.path}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </Section>
  );
}

function PolicyTab({
  policy,
  policyEtag,
  canWrite,
  subject,
  notify,
  onSaved,
}: {
  policy: Record<string, unknown>;
  policyEtag: string | null;
  canWrite: boolean;
  subject: GovernanceSubjectInput;
  notify: Notify;
  onSaved: () => void;
}) {
  const [policyText, setPolicyText] = useState(() => pretty(policy));
  const [preview, setPreview] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);

  async function previewPolicy() {
    setPreviewing(true);
    try {
      const parsed = JSON.parse(policyText) as Record<string, unknown>;
      const res = await api.previewGovernancePolicy(parsed, subject);
      setPreview(pretty(res.effective_access));
      notify("Preview updated.", "success");
    } catch (err) {
      notify(describeApiError(err), "error");
    } finally {
      setPreviewing(false);
    }
  }

  async function savePolicy() {
    setSaving(true);
    try {
      const parsed = JSON.parse(policyText) as Record<string, unknown>;
      // Pass the etag of the snapshot this editor loaded; the backend
      // rejects the save (412) if the policy changed underneath us so a
      // stale full-replace can't silently drop concurrent entry edits.
      await api.saveGovernancePolicy(parsed, policyEtag);
      notify("Policy saved.", "success");
      onSaved();
    } catch (err) {
      notify(describeApiError(err), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Section
      title="Policy JSON"
      actions={
        <div className="flex items-center gap-2">
          <Button disabled={previewing} onClick={() => void previewPolicy()} size="sm">
            {previewing ? "Previewing…" : "Preview"}
          </Button>
          <Button disabled={!canWrite || saving} onClick={() => void savePolicy()} size="sm">
            {saving ? "Saving…" : "Save policy"}
          </Button>
        </div>
      }
    >
      <textarea
        className="min-h-[22rem] w-full rounded border border-current/15 bg-background-base p-3 font-mono text-xs text-text-primary outline-none focus:ring-1 focus:ring-midground disabled:opacity-60"
        disabled={!canWrite}
        onChange={(event) => setPolicyText(event.target.value)}
        spellCheck={false}
        value={policyText}
      />
      {preview && (
        <div className="mt-4 rounded border border-current/15 bg-background-base p-3">
          <h3 className="mb-2 font-sans text-display text-xs tracking-[0.12em] text-text-secondary">
            Preview for current subject
          </h3>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-xs text-text-secondary">
            {preview}
          </pre>
        </div>
      )}
    </Section>
  );
}

export default function GovernancePage() {
  const { access, refresh, hasPermission } = useGovernance();
  const { toast, showToast } = useToast();
  const [tab, setTab] = useState<GovernanceTab>("overview");
  const [loading, setLoading] = useState(true);
  const [policy, setPolicy] = useState<Record<string, unknown>>({});
  const [policyEtag, setPolicyEtag] = useState<string | null>(null);
  const [users, setUsers] = useState<Record<string, GovernanceUserEntry>>({});
  const [groups, setGroups] = useState<Record<string, GovernanceGroupEntry>>({});
  const [audit, setAudit] = useState<GovernanceAuditEvent[]>([]);
  const [deniedLast24h, setDeniedLast24h] = useState(0);
  const [usage, setUsage] = useState<Record<string, unknown>>({});
  const [refreshingAudit, setRefreshingAudit] = useState(false);

  const canWrite = hasPermission("governance:write");
  const canAudit = hasPermission("governance:audit:read");
  const canUsage = hasPermission("governance:usage:read");
  const canPreview = hasPermission("governance:preview");

  const load = useCallback(async () => {
    try {
      const [policyRes, usersRes, groupsRes, auditRes, usageRes] = await Promise.all([
        api
          .getGovernancePolicy()
          .catch(() => ({ policy: {} as Record<string, unknown>, etag: null as string | null })),
        api.getGovernanceUsers().catch(() => ({}) as Record<string, GovernanceUserEntry>),
        api.getGovernanceGroups().catch(() => ({}) as Record<string, GovernanceGroupEntry>),
        canAudit
          ? api.getGovernanceAudit(200).catch(() => ({ events: [] }))
          : Promise.resolve({ events: [] as GovernanceAuditEvent[] }),
        canUsage
          ? api.getGovernanceUsage().catch(() => ({ usage: {} }))
          : Promise.resolve({ usage: {} as Record<string, unknown> }),
      ]);
      setPolicy(policyRes.policy);
      setPolicyEtag(policyRes.etag);
      setUsers(usersRes);
      setGroups(groupsRes);
      setAudit(auditRes.events ?? []);
      setDeniedLast24h(countDeniedLast24h(auditRes.events ?? []));
      setUsage(usageRes.usage ?? {});
    } finally {
      setLoading(false);
    }
  }, [canAudit, canUsage]);

  useEffect(() => {
    void load();
  }, [load]);

  const refreshPolicy = useCallback(async () => {
    try {
      const res = await api.getGovernancePolicy();
      setPolicy(res.policy);
      setPolicyEtag(res.etag);
    } catch {
      /* keep the last known policy */
    }
  }, []);

  async function reloadAudit() {
    setRefreshingAudit(true);
    try {
      const res = await api.getGovernanceAudit(200);
      setAudit(res.events ?? []);
      setDeniedLast24h(countDeniedLast24h(res.events ?? []));
    } catch (err) {
      showToast(describeApiError(err), "error");
    } finally {
      setRefreshingAudit(false);
    }
  }

  const tabOptions = useMemo(() => {
    const options: { label: string; value: GovernanceTab }[] = [
      { label: "Overview", value: "overview" },
      { label: "Users", value: "users" },
      { label: "Groups", value: "groups" },
      { label: "Resources", value: "resources" },
    ];
    if (canPreview) options.push({ label: "Preview Access", value: "preview" });
    if (canAudit) options.push({ label: "Audit", value: "audit" });
    options.push({ label: "Policy", value: "policy" });
    return options;
  }, [canPreview, canAudit]);

  const grantSummary = useMemo(
    () =>
      [
        `${access?.mode ?? "unknown"} mode`,
        `${access?.roles?.length ?? 0} roles`,
        `${access?.permissions?.length ?? 0} permissions`,
        `${access?.profiles?.length ?? 0} profiles`,
      ].join(" · "),
    [access],
  );

  return (
    <div className="space-y-6 text-text-primary">
      <header className="flex flex-col gap-2">
        <div className="flex items-center gap-3">
          <ShieldCheck className="h-6 w-6 text-midground" />
          <h1 className="font-sans text-display text-2xl tracking-[0.08em]">Governance</h1>
        </div>
        <p className="text-sm text-text-secondary">{grantSummary}</p>
      </header>

      {loading ? (
        <div className="flex items-center gap-2 text-sm text-text-secondary">
          <Spinner /> Loading governance…
        </div>
      ) : (
        <>
          <Segmented onChange={setTab} options={tabOptions} size="md" value={tab} />

          {tab === "overview" && (
            <OverviewTab
              access={access}
              canAudit={canAudit}
              canUsage={canUsage}
              deniedLast24h={deniedLast24h}
              groups={groups}
              usage={usage}
              users={users}
            />
          )}

          {tab === "users" && (
            <UsersTab
              canWrite={canWrite}
              notify={showToast}
              onChanged={(next) => {
                setUsers(next);
                void refreshPolicy();
              }}
              users={users}
            />
          )}

          {tab === "groups" && (
            <GroupsTab
              canWrite={canWrite}
              groups={groups}
              notify={showToast}
              onChanged={(next) => {
                setGroups(next);
                void refreshPolicy();
              }}
            />
          )}

          {tab === "resources" && <ResourcesTab groups={groups} policy={policy} users={users} />}

          {tab === "preview" && canPreview && (
            <PreviewTab
              defaultEmail={access?.subject?.email ?? ""}
              notify={showToast}
              policy={policy}
            />
          )}

          {tab === "audit" && canAudit && (
            <AuditTab events={audit} onRefresh={() => void reloadAudit()} refreshing={refreshingAudit} />
          )}

          {tab === "policy" && (
            <PolicyTab
              canWrite={canWrite}
              notify={showToast}
              onSaved={() => {
                void refresh();
                void load();
              }}
              policy={policy}
              policyEtag={policyEtag}
              subject={access?.subject ?? {}}
            />
          )}
        </>
      )}

      <Toast toast={toast} />
    </div>
  );
}
