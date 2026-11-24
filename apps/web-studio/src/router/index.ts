import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import { ref, type Ref } from "vue";
import { ApiError, request } from "../services/http";
import { resolveNavigation } from "./guard";
import { settingsRoutes } from "../settings/routes";
import LoginView from "../components/LoginView.vue";
import WorkspaceView from "../components/WorkspaceView.vue";
import Metal3FleetView from "../components/Metal3FleetView.vue";
import type { CurrentUser } from "../services/api";

type AuthStatus = "unknown" | "authenticated" | "unauthenticated" | "forbidden";

export const authStatus: Ref<AuthStatus> = ref("unknown");
export const currentUser: Ref<CurrentUser | null> = ref(null);

type WorkspaceSection =
  | "overview"
  | "chat"
  | "models"
  | "agents"
  | "teams"
  | "workflows"
  | "integrations"
  | "marketplace"
  | "knowledge"
  | "registry"
  | "governance"
  | "audit"
  | "organization"
  | "docs";

const WORKSPACE_SECTIONS: { path: string; section: WorkspaceSection }[] = [
  { path: "/", section: "overview" },
  { path: "/chat", section: "chat" },
  { path: "/models", section: "models" },
  { path: "/agents", section: "agents" },
  { path: "/teams", section: "teams" },
  { path: "/workflows", section: "workflows" },
  { path: "/integrations", section: "integrations" },
  { path: "/marketplace", section: "marketplace" },
  { path: "/knowledge", section: "knowledge" },
  { path: "/registry", section: "registry" },
  { path: "/governance", section: "governance" },
  { path: "/audit", section: "audit" },
  { path: "/organization", section: "organization" },
  { path: "/docs", section: "docs" },
];

const legacyRoutes: RouteRecordRaw[] = [
  { path: "/login", name: "login", component: LoginView, meta: {} },
  ...WORKSPACE_SECTIONS.map(({ path, section }) => ({
    path,
    name: `workspace-${section}`,
    component: WorkspaceView,
    props: { activeSection: section },
    meta: { section },
  })),
  {
    path: "/infrastructure",
    name: "infrastructure",
    component: Metal3FleetView,
    meta: { section: "overview" },
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes: [...legacyRoutes, ...settingsRoutes],
});

let probePromise: Promise<void> | null = null;

/** Probes `/api/v1/auth/me/` once and caches the result; 401 is anonymous, 403 is forbidden. */
async function probeAuth(): Promise<void> {
  if (probePromise) return probePromise;
  probePromise = (async () => {
    try {
      const user = await request<CurrentUser>("/api/v1/auth/me/");
      currentUser.value = user;
      authStatus.value = "authenticated";
    } catch (cause) {
      currentUser.value = null;
      if (cause instanceof ApiError && cause.status === 403) {
        authStatus.value = "forbidden";
      } else {
        authStatus.value = "unauthenticated";
      }
    }
  })();
  return probePromise;
}

router.beforeEach(async (to) => {
  await probeAuth();
  const decision = resolveNavigation(to, {
    authenticated: authStatus.value === "authenticated",
    loading: authStatus.value === "unknown",
    forbidden: authStatus.value === "forbidden",
  });
  if (decision.kind === "redirect-login") return decision.next;
  if (decision.kind === "redirect-home") return "/";
  return true;
});

export function setAuthenticated(user: CurrentUser): void {
  currentUser.value = user;
  authStatus.value = "authenticated";
}

export function resetAuthCache(): void {
  probePromise = null;
  currentUser.value = null;
  authStatus.value = "unknown";
}
