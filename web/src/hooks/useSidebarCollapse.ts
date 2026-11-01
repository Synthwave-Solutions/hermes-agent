import { useCallback, useState } from "react";

const SIDEBAR_COLLAPSED_KEY = "hermes-sidebar-collapsed";

/** Persist the desktop sidebar preference; storage may be disabled by the browser. */
export function useSidebarCollapse() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "true";
    } catch {
      return false;
    }
  });
  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next));
      } catch { /* localStorage may be unavailable in private browsing */ }
      return next;
    });
  }, []);
  return { collapsed, toggleCollapsed };
}
