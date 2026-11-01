type AuthenticatedFetch = <T>(path: string, init?: RequestInit,
  options?: { allowUnauthorized?: boolean }) => Promise<T>;

/** Keep auth probes and logout on the dashboard's authenticated transport and base path. */
export function createAuthApi(fetchJSON: AuthenticatedFetch, basePath: string) {
  return {
  /**
   * Identity probe for the dashboard auth gate (Phase 7).
   *
   * Returns the verified Session when gated mode is active. In loopback mode,
   * a valid injected session token returns a synthetic ``provider=loopback``
   * identity; AuthWidget recognizes that provider and renders nothing, so it
   * never exposes a logout action that would redirect to an unavailable login.
   *
   * ``allowUnauthorized`` remains load-bearing for a missing or stale loopback
   * token: keep that 401 local to the widget instead of triggering the global
   * rotated-token reload path.
   */
  getAuthMe: () =>
    fetchJSON<AuthMeResponse>("/api/auth/me", undefined, {
      allowUnauthorized: true,
    }),
  logout: () =>
    fetch(`${basePath}/auth/logout`, {
      method: "POST",
      credentials: "include",
    }).then((r) => {
      // /auth/logout returns 302 → /login. Follow that with a full-page
      // navigation rather than letting fetch() opaquely consume the
      // redirect — the SPA needs to leave the protected area.
      window.location.assign(`${basePath}/login`);
      return r;
    }),
  };
}

/** Identity payload returned by ``GET /api/auth/me`` (Phase 7).
 *
 * Returned by the dashboard's gated middleware when a valid session cookie
 * is attached, or as a synthetic ``provider=loopback`` identity after token
 * validation in local mode. ``email`` and ``display_name`` are empty strings under the
 * Nous Portal contract V1 (the access token has no email/name claims —
 * see Contract Anchor C4 in the plan). The AuthWidget surfaces a
 * truncated ``user_id`` instead.
 */
export interface AuthMeResponse {
  user_id: string;
  email: string;
  display_name: string;
  org_id: string;
  provider: string;
  expires_at: number;
}
