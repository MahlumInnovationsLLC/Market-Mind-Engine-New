/**
 * Best-effort, Clerk-free signed-in detection used by the public
 * marketing surface so it can render meaningful content (and the
 * correct CTA) without waiting for the full Clerk SDK chunk to load.
 *
 * Clerk sets `__client_uat` (user-authenticated-time) as a non-secret
 * cookie that is `0` when signed-out and a unix timestamp > 0 when
 * a session is active. The actual JWT lives in a separate, secret
 * cookie that is still verified server-side on every authed request,
 * so this heuristic is safe to use for UI affordances only.
 */
export function hasClerkSession(): boolean {
  if (typeof document === "undefined") return false;
  const cookies = document.cookie.split(";");
  for (const raw of cookies) {
    const [name, value] = raw.trim().split("=");
    if (name === "__client_uat" || name?.startsWith("__client_uat_")) {
      const n = Number(value);
      if (Number.isFinite(n) && n > 0) return true;
    }
  }
  return false;
}
