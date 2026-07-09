import {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api, type GovernanceEffectiveAccessResponse } from "@/lib/api";

interface GovernanceContextValue {
  access: GovernanceEffectiveAccessResponse | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  hasPermission: (permission: string) => boolean;
  canRoute: (path: string) => boolean;
  canProfile: (profile: string) => boolean;
}

const fallbackAccess: GovernanceEffectiveAccessResponse = {
  mode: "off",
  subject: {},
  roles: [],
  groups: [],
  permissions: ["*"],
  profiles: ["*"],
  routes: ["*"],
  grant_sources: [],
  is_admin: true,
};

export const GovernanceContext = createContext<GovernanceContextValue>({
  access: fallbackAccess,
  loading: false,
  error: null,
  refresh: async () => {},
  hasPermission: () => true,
  canRoute: () => true,
  canProfile: () => true,
});

function setAllows(values: Set<string>, value: string): boolean {
  if (values.has("*")) return true;
  if (values.has(value)) return true;
  return [...values].some((entry) => entry.endsWith("*") && value.startsWith(entry.slice(0, -1)));
}

export function GovernanceProvider({ children }: { children: ReactNode }) {
  const [access, setAccess] = useState<GovernanceEffectiveAccessResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setAccess(await api.getGovernanceEffectiveAccess());
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
    void refresh();
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
