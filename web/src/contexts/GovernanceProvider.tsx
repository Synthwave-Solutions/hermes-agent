import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, type GovernanceEffectiveAccessResponse } from "@/lib/api";
import { GovernanceContext, fallbackAccess } from "@/contexts/governance-context";

function setAllows(values: Set<string>, value: string): boolean {
  if (values.has("*")) return true;
  if (values.has(value)) return true;
  return [...values].some((entry) => entry.endsWith("*") && value.startsWith(entry.slice(0, -1)));
}

export function GovernanceProvider({ children }: { children: ReactNode }) {
  const [access, setAccess] = useState<GovernanceEffectiveAccessResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // All state updates happen after the fetch settles (never synchronously in
  // the mount effect); `loading` starts true and only flips off here.
  const refresh = useCallback(async () => {
    try {
      const next = await api.getGovernanceEffectiveAccess();
      setAccess(next);
      setError(null);
    } catch (err) {
      // Governance is optional/backwards compatible. If the endpoint is missing
      // or unavailable, fail open in the SPA; the backend remains authoritative.
      setAccess(fallbackAccess);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Defer to a microtask so no setState runs synchronously in the effect
    // body (react-hooks/set-state-in-effect).
    void Promise.resolve().then(refresh);
  }, [refresh]);

  const value = useMemo(() => {
    const effective = access ?? fallbackAccess;
    const permissions = new Set(effective.permissions ?? []);
    const routes = new Set(effective.routes ?? []);
    const profiles = new Set(effective.profiles ?? []);
    return {
      access: effective,
      loading,
      error,
      refresh,
      hasPermission: (permission: string) => setAllows(permissions, permission),
      canRoute: (path: string) => setAllows(routes, path),
      canProfile: (profile: string) => setAllows(profiles, profile || "default"),
    };
  }, [access, error, loading, refresh]);

  return <GovernanceContext.Provider value={value}>{children}</GovernanceContext.Provider>;
}
