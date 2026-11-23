import type { RouteRecordRaw } from "vue-router";
import { SETTINGS_GROUPS, SETTINGS_SLUGS } from "./shared/scope.ts";

const GROUP_BY_SLUG: Record<string, "account" | "general"> = {};
for (const group of SETTINGS_GROUPS) {
  for (const slug of group.slugs) GROUP_BY_SLUG[slug] = group.group;
}

export interface SettingsRouteDef {
  slug: string;
  path: string;
  name: string;
  group: "account" | "general";
  section: string;
  file: string;
  meta: { requiresAuth: true; group: "account" | "general"; section: string };
}

export const settingsRouteDefs: SettingsRouteDef[] = SETTINGS_SLUGS.map((slug) => ({
  slug,
  path: `/settings/${slug}`,
  name: `settings.${slug}`,
  group: GROUP_BY_SLUG[slug],
  section: slug,
  file: `${slug}/index.vue`,
  meta: { requiresAuth: true, group: GROUP_BY_SLUG[slug], section: slug },
}));

const componentLoaders: Record<string, () => Promise<unknown>> = {
  account: () => import("./account/index.vue"),
  plan: () => import("./plan/index.vue"),
  subscription: () => import("./subscription/index.vue"),
  general: () => import("./general/index.vue"),
  connectors: () => import("./connectors/index.vue"),
  appearance: () => import("./appearance/index.vue"),
  notifications: () => import("./notifications/index.vue"),
  "work-mode": () => import("./work-mode/index.vue"),
  shortcuts: () => import("./shortcuts/index.vue"),
  "data-controls": () => import("./data-controls/index.vue"),
  archive: () => import("./archive/index.vue"),
  privacy: () => import("./privacy/index.vue"),
  help: () => import("./help/index.vue"),
  about: () => import("./about/index.vue"),
};

export const settingsRoutes: RouteRecordRaw[] = [
  {
    path: "/settings",
    name: "settings",
    redirect: "/settings/account",
    component: () => import("./layout/SettingsLayout.vue"),
    meta: { requiresAuth: true },
    children: settingsRouteDefs.map((def) => ({
      path: def.slug,
      name: def.name,
      meta: def.meta,
      component: componentLoaders[def.slug],
    })),
  },
];
