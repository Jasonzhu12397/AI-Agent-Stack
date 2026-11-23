/**
 * Pure navigation guard decisions. The router only reads this module's
 * `resolveNavigation`; authorization is enforced here, never by hiding menu items.
 */

export interface GuardRoute {
  path: string;
  fullPath: string;
  name?: unknown;
  meta: Record<string, unknown>;
}

export type GuardDecision =
  | { kind: "allow" }
  | { kind: "redirect-login"; next: string }
  | { kind: "redirect-home" }
  | { kind: "forbidden" };

export interface GuardContext {
  authenticated: boolean;
  loading: boolean;
  /** Set when the auth probe returned 403: the member is logged in but lacks the role. */
  forbidden?: boolean;
}

export function resolveNavigation(to: GuardRoute, ctx: GuardContext): GuardDecision {
  if (ctx.loading) return { kind: "allow" };
  if (ctx.forbidden) return { kind: "forbidden" };

  const requiresAuth = to.meta.requiresAuth === true;
  if (requiresAuth && !ctx.authenticated) {
    const next = encodeURIComponent(to.fullPath);
    return { kind: "redirect-login", next: `/login?next=${next}` };
  }
  if (to.path === "/login" && ctx.authenticated) {
    return { kind: "redirect-home" };
  }
  return { kind: "allow" };
}
