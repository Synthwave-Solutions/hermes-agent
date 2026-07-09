import { useContext } from "react";
import { GovernanceContext } from "@/contexts/GovernanceProvider";

export function useGovernance() {
  return useContext(GovernanceContext);
}
