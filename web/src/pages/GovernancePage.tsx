import { useEffect, useMemo, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api, type GovernanceAuditEvent } from "@/lib/api";
import { useGovernance } from "@/contexts/useGovernance";

function pretty(value: unknown): string {
  return JSON.stringify(value ?? {}, null, 2);
}

export default function GovernancePage() {
  const { access, refresh, hasPermission } = useGovernance();
  const [policyText, setPolicyText] = useState("{}");
  const [audit, setAudit] = useState<GovernanceAuditEvent[]>([]);
  const [usage, setUsage] = useState<Record<string, unknown>>({});
  const [preview, setPreview] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const canWrite = hasPermission("governance:write");
  const canAudit = hasPermission("governance:audit:read");
  const canUsage = hasPermission("governance:usage:read");

  const grantSummary = useMemo(
    () => [
      `${access?.mode ?? "unknown"} mode`,
      `${access?.roles?.length ?? 0} roles`,
      `${access?.permissions?.length ?? 0} permissions`,
      `${access?.profiles?.length ?? 0} profiles`,
    ].join(" · "),
    [access],
  );

  async function load() {
    setLoading(true);
    setMessage(null);
    try {
      const [policy, auditRes, usageRes] = await Promise.all([
        api.getGovernancePolicy().catch(() => ({})),
        canAudit ? api.getGovernanceAudit(50).catch(() => ({ events: [] })) : Promise.resolve({ events: [] }),
        canUsage ? api.getGovernanceUsage().catch(() => ({ usage: {} })) : Promise.resolve({ usage: {} }),
      ]);
      setPolicyText(pretty(policy));
      setAudit(auditRes.events ?? []);
      setUsage(usageRes.usage ?? {});
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canAudit, canUsage]);

  async function previewPolicy() {
    setPreviewing(true);
    setMessage(null);
    try {
      const parsed = JSON.parse(policyText) as Record<string, unknown>;
      const res = await api.previewGovernancePolicy(parsed, access?.subject ?? {});
      setPreview(pretty(res.effective_access));
      setMessage("Preview updated.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setPreviewing(false);
    }
  }

  async function savePolicy() {
    setSaving(true);
    setMessage(null);
    try {
      const parsed = JSON.parse(policyText) as Record<string, unknown>;
      await api.saveGovernancePolicy(parsed);
      await refresh();
      setMessage("Policy saved.");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

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
        <div className="flex items-center gap-2 text-sm text-text-secondary"><Spinner /> Loading governance…</div>
      ) : (
        <>
          <section className="rounded-lg border border-current/15 bg-background/60 p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="font-sans text-display text-sm tracking-[0.12em]">Policy JSON</h2>
              <div className="flex items-center gap-2">
                <Button disabled={previewing} onClick={previewPolicy} size="sm">
                  {previewing ? "Previewing…" : "Preview"}
                </Button>
                <Button disabled={!canWrite || saving} onClick={savePolicy} size="sm">
                  {saving ? "Saving…" : "Save policy"}
                </Button>
              </div>
            </div>
            <textarea
              className="min-h-[22rem] w-full rounded border border-current/15 bg-background-base p-3 font-mono text-xs text-text-primary outline-none focus:ring-1 focus:ring-midground disabled:opacity-60"
              disabled={!canWrite}
              onChange={(event) => setPolicyText(event.target.value)}
              spellCheck={false}
              value={policyText}
            />
            {message && <p className="mt-2 text-sm text-text-secondary">{message}</p>}
            {preview && (
              <div className="mt-4 rounded border border-current/15 bg-background-base p-3">
                <h3 className="mb-2 font-sans text-display text-xs tracking-[0.12em] text-text-secondary">Preview for current subject</h3>
                <pre className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-xs text-text-secondary">{preview}</pre>
              </div>
            )}
          </section>

          <section className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-lg border border-current/15 bg-background/60 p-4">
              <h2 className="mb-3 font-sans text-display text-sm tracking-[0.12em]">Recent audit</h2>
              <div className="max-h-80 overflow-auto font-mono text-xs text-text-secondary">
                {audit.length === 0 ? "No audit events visible." : audit.map((event) => (
                  <div key={`${event.ts}-${event.event}-${event.path}`} className="border-b border-current/10 py-2">
                    <div>{event.ts} · {event.event} · {event.reason}</div>
                    <div>{event.method} {event.path}</div>
                  </div>
                ))}
              </div>
            </div>
            <div className="rounded-lg border border-current/15 bg-background/60 p-4">
              <h2 className="mb-3 font-sans text-display text-sm tracking-[0.12em]">Usage caps</h2>
              <pre className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-xs text-text-secondary">{pretty(usage)}</pre>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
