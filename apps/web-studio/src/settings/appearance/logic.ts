export type AppearanceSettings = {
  theme: "light" | "dark" | "system";
  density: "comfortable" | "compact";
  font_scale: number;
  reduced_motion: boolean;
};

export const APPEARANCE_DEFAULTS: AppearanceSettings = {
  theme: "light",
  density: "comfortable",
  font_scale: 1,
  reduced_motion: false,
};

export const APPEARANCE_STORAGE_KEY = "ai-agent-studio-appearance";
export const FONT_SCALE_MIN = 0.875;
export const FONT_SCALE_MAX = 1.5;

const THEMES = ["light", "dark", "system"] as const;
const DENSITIES = ["comfortable", "compact"] as const;

/** Deterministic normalisation: unknown or corrupt input always collapses to the defaults. */
export function normalizeAppearance(raw: unknown): AppearanceSettings {
  const result: AppearanceSettings = { ...APPEARANCE_DEFAULTS };
  if (!raw || typeof raw !== "object") return result;
  const value = raw as Record<string, unknown>;
  if (typeof value.theme === "string" && (THEMES as readonly string[]).includes(value.theme)) {
    result.theme = value.theme as AppearanceSettings["theme"];
  }
  if (typeof value.density === "string" && (DENSITIES as readonly string[]).includes(value.density)) {
    result.density = value.density as AppearanceSettings["density"];
  }
  if (typeof value.font_scale === "number" && Number.isFinite(value.font_scale)) {
    result.font_scale = clampFontScale(value.font_scale);
  }
  if (typeof value.reduced_motion === "boolean") result.reduced_motion = value.reduced_motion;
  return result;
}

export function clampFontScale(value: number): number {
  if (!Number.isFinite(value)) return APPEARANCE_DEFAULTS.font_scale;
  const rounded = Math.round(value * 1000) / 1000;
  return Math.min(FONT_SCALE_MAX, Math.max(FONT_SCALE_MIN, rounded));
}

export function validateAppearance(values: AppearanceSettings): { ok: boolean; errors: Record<string, string> } {
  const errors: Record<string, string> = {};
  if (!(THEMES as readonly string[]).includes(values.theme)) errors.theme = "APPEARANCE_INVALID_THEME";
  if (!(DENSITIES as readonly string[]).includes(values.density)) errors.density = "APPEARANCE_INVALID_DENSITY";
  if (!Number.isFinite(values.font_scale) || values.font_scale < FONT_SCALE_MIN || values.font_scale > FONT_SCALE_MAX) {
    errors.font_scale = "APPEARANCE_INVALID_FONT_SCALE";
  }
  if (typeof values.reduced_motion !== "boolean") errors.reduced_motion = "APPEARANCE_INVALID_REDUCED_MOTION";
  return { ok: Object.keys(errors).length === 0, errors };
}

export type AppearanceStorage = { getItem(key: string): string | null } | null;

/**
 * Browser storage may be absent (private mode, quota, disabled cookies). Any failure falls back
 * to defaults instead of throwing, so the page still renders deterministically.
 */
export function readCachedAppearance(storage?: AppearanceStorage): AppearanceSettings {
  if (!storage) return { ...APPEARANCE_DEFAULTS };
  let raw: string | null = null;
  try {
    raw = storage.getItem(APPEARANCE_STORAGE_KEY);
  } catch {
    return { ...APPEARANCE_DEFAULTS };
  }
  if (!raw) return { ...APPEARANCE_DEFAULTS };
  try {
    return normalizeAppearance(JSON.parse(raw));
  } catch {
    return { ...APPEARANCE_DEFAULTS };
  }
}

export type AppearanceRoot = {
  dataset: Record<string, string | undefined>;
  style: { setProperty(name: string, value: string): void };
};

export function applyAppearance(settings: AppearanceSettings, root: AppearanceRoot): void {
  const normalized = normalizeAppearance(settings);
  root.dataset.theme = normalized.theme;
  root.dataset.density = normalized.density;
  root.dataset.reducedMotion = normalized.reduced_motion ? "true" : "false";
  root.style.setProperty("--app-font-scale", String(normalized.font_scale));
}

export function resolveTheme(theme: AppearanceSettings["theme"], prefersDark: boolean): "light" | "dark" {
  if (theme === "system") return prefersDark ? "dark" : "light";
  return theme;
}

export function describeError(code: string): string {
  switch (code) {
    case "APPEARANCE_INVALID_FONT_SCALE":
      return `Font scale must stay between ${FONT_SCALE_MIN} and ${FONT_SCALE_MAX}.`;
    case "HTTP_401":
      return "Your session has expired. Sign in again.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export const pageLabels = [
  "Appearance",
  "Theme",
  "Light",
  "Dark",
  "System",
  "Density",
  "Comfortable",
  "Compact",
  "Font scale",
  "Reduce motion",
  "Preferences are stored per user",
  "Saved on this device only",
];
