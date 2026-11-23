import test from "node:test";
import assert from "node:assert/strict";
import { resolveNavigation } from "../src/router/guard.ts";

const protectedRoute = (overrides = {}) => ({
  path: "/settings/general",
  fullPath: "/settings/general",
  meta: { requiresAuth: true, group: "general", section: "general" },
  ...overrides,
});

test("public route is allowed for anonymous visitors", () => {
  const decision = resolveNavigation({ path: "/", fullPath: "/", meta: {} }, { authenticated: false, loading: false });
  assert.equal(decision.kind, "allow");
});

test("unauthenticated access to a protected route redirects to login with next", () => {
  const decision = resolveNavigation(protectedRoute(), { authenticated: false, loading: false });
  assert.equal(decision.kind, "redirect-login");
  assert.ok(decision.next.startsWith("/login?next="));
  assert.ok(decision.next.includes(encodeURIComponent("/settings/general")));
});

test("navigation stays allowed while the auth probe is loading", () => {
  const decision = resolveNavigation(protectedRoute(), { authenticated: false, loading: true });
  assert.equal(decision.kind, "allow");
});

test("authenticated visitor on /login bounces to home", () => {
  const decision = resolveNavigation(
    { path: "/login", fullPath: "/login", meta: {} },
    { authenticated: true, loading: false },
  );
  assert.equal(decision.kind, "redirect-home");
});

test("forbidden member is denied on protected routes", () => {
  const decision = resolveNavigation(protectedRoute(), { authenticated: true, loading: false, forbidden: true });
  assert.equal(decision.kind, "forbidden");
});

test("anonymous visitor may open the login page", () => {
  const decision = resolveNavigation(
    { path: "/login", fullPath: "/login", meta: {} },
    { authenticated: false, loading: false },
  );
  assert.equal(decision.kind, "allow");
});
