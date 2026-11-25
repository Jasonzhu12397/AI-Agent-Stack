export type SettingsLoadState = "loading" | "ready" | "empty" | "error" | "denied";

export type SettingsScope = "user" | "tenant" | "project";

export type ScopeLayer = "user-local" | "project" | "tenant" | "default";

export const SETTINGS_SLUGS = [
  "account",
  "plan",
  "subscription",
  "general",
  "appearance",
  "notifications",
  "work-mode",
  "shortcuts",
  "data-controls",
  "archive",
  "privacy",
  "help",
  "about",
] as const;

export type SettingsSlug = (typeof SETTINGS_SLUGS)[number];

export const SETTINGS_GROUPS: { group: "account" | "general"; slugs: SettingsSlug[] }[] = [
  { group: "account", slugs: ["account", "plan", "subscription"] },
  {
    group: "general",
    slugs: [
      "general",
          "appearance",
      "notifications",
      "work-mode",
      "shortcuts",
      "data-controls",
      "archive",
      "privacy",
      "help",
      "about",
    ],
  },
];

export const SETTINGS_LABELS: Record<SettingsSlug, string> = {
  account: "Account Management",
  plan: "Standard Plan",
  subscription: "Subscription & Usage Management",
  general: "General",
  appearance: "Appearance",
  notifications: "Notification Settings",
  "work-mode": "Work Mode Settings",
  shortcuts: "Keyboard Shortcuts",
  "data-controls": "Data Controls",
  archive: "Archived Content",
  privacy: "Privacy & Permissions",
  help: "Help & Feedback",
  about: "About",
};

/**
 * Folds layered settings values. Precedence (documented and tested): project > tenant,
 * user-local overrides both for the namespaces it owns; otherwise the code default wins.
 */
export function foldScopes(
  defaults: Record<string, unknown>,
  layers: Partial<Record<ScopeLayer, Record<string, unknown>>>,
): { values: Record<string, unknown>; sources: Record<string, ScopeLayer> } {
  const values: Record<string, unknown> = { ...defaults };
  const sources: Record<string, ScopeLayer> = {};
  for (const key of Object.keys(defaults)) sources[key] = "default";
  for (const layer of ["tenant", "project", "user-local"] as ScopeLayer[]) {
    const values_ = layers[layer];
    if (!values_) continue;
    for (const [key, value] of Object.entries(values_)) {
      if (value === undefined) continue;
      values[key] = value;
      sources[key] = layer;
    }
  }
  return { values, sources };
}

export function scopeLabel(layer: ScopeLayer): string {
  switch (layer) {
    case "user-local":
      return "User preference";
    case "project":
      return "Project override";
    case "tenant":
      return "Tenant setting";
    default:
      return "Default";
  }
}
