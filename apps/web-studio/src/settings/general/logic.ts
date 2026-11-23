import { foldScopes, type ScopeLayer } from "../shared/scope.ts";
import { ApiError } from "../../services/http.ts";

export type GeneralScope = "tenant" | "project";

export type GeneralEnvelope = {
  section: string;
  scope: GeneralScope;
  values: Record<string, unknown>;
  effective: Record<string, unknown>;
  defaults: Record<string, unknown>;
  updated_at: string | null;
};

/** English source strings rendered on the General page; keys must exist in the zh namespace. */
export const pageLabels: Record<string, string> = {
  "General": "General",
  "General settings": "General settings",
  "Scope": "Scope",
  "Tenant": "Tenant",
  "Project": "Project",
  "Effective value": "Effective value",
  "Default": "Default",
  "Source layer": "Source layer",
  "Updated": "Updated",
  "Saved": "Saved",
  "Save changes": "Save changes",
  "Defaults": "Defaults",
  "Effective": "Effective",
  "No settings available for this scope.": "No settings available for this scope.",
  "Tenant setting": "Tenant setting",
  "Project override": "Project override",
  "User preference": "User preference",
  "Unknown setting key.": "Unknown setting key.",
  "Invalid value for setting.": "Invalid value for setting.",
  "Settings could not be saved.": "Settings could not be saved.",
};

export function generalScopePath(scope: GeneralScope, tenantId: string, projectId: string): string {
  if (scope === "project") {
    return `/api/v1/tenants/${tenantId}/projects/${projectId}/settings/general/`;
  }
  return `/api/v1/tenants/${tenantId}/settings/general/`;
}

export function foldEffective(
  defaults: Record<string, unknown>,
  layers: Partial<Record<ScopeLayer, Record<string, unknown>>>,
) {
  return foldScopes(defaults, layers);
}

/** Keys where the locally folded values diverge from the server-reported `effective`. */
export function compareEffective(
  folded: { values: Record<string, unknown> },
  effective: Record<string, unknown>,
): string[] {
  const keys = new Set([...Object.keys(folded.values), ...Object.keys(effective)]);
  const mismatches: string[] = [];
  for (const key of keys) {
    if (JSON.stringify(folded.values[key]) !== JSON.stringify(effective[key])) {
      mismatches.push(key);
    }
  }
  return mismatches;
}

function typeOf(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

/** Validates submitted values against the server-provided defaults; unknown keys and type mismatches fail closed. */
export function validateGeneralValues(
  values: Record<string, unknown>,
  defaults: Record<string, unknown>,
): Record<string, string[]> {
  const errors: Record<string, string[]> = {};
  for (const [key, value] of Object.entries(values)) {
    if (!(key in defaults)) {
      errors[key] = ["SETTINGS_UNKNOWN_KEY"];
      continue;
    }
    if (typeOf(defaults[key]) !== typeOf(value)) {
      errors[key] = ["SETTINGS_INVALID_VALUE"];
    }
  }
  return errors;
}

export function fieldErrorsFromError(error: ApiError): Record<string, string[]> {
  const details = error.details ?? {};
  const out: Record<string, string[]> = {};
  for (const [key, value] of Object.entries(details)) {
    out[key] = Array.isArray(value) ? value.map(String) : [String(value)];
  }
  return out;
}

export function stateFromError(error: unknown): { kind: "error" | "denied" } {
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
    return { kind: "denied" };
  }
  return { kind: "error" };
}

const ERROR_MESSAGE_KEYS: Record<string, string> = {
  SETTINGS_UNKNOWN_KEY: "Unknown setting key.",
  SETTINGS_INVALID_VALUE: "Invalid value for setting.",
};

export function errorMessageKey(code: string): string {
  return ERROR_MESSAGE_KEYS[code] ?? "Settings could not be saved.";
}
