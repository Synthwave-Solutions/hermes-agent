import { createContext } from "react";
import type { GovernanceEffectiveAccessResponse } from "@/lib/api";

export interface GovernanceContextValue {
  access: GovernanceEffectiveAccessResponse | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  hasPermission: (permission: string) => boolean;
  canRoute: (path: string) => boolean;
  canProfile: (profile: string) => boolean;
}

/** Fail-open default: governance is optional/backwards compatible, so an
 *  absent provider (or endpoint) behaves like governance off. The backend
 *  remains authoritative for every actual API call. */
export const fallbackAccess: GovernanceEffectiveAccessResponse = {
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
