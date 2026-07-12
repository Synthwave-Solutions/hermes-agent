import { useContext } from "react";
import { GovernanceContext } from "@/contexts/governance-context";

export function useGovernance() {
  return useContext(GovernanceContext);
}
