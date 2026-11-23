import { request } from "../../services/http";
import type { ShortcutBindings, ShortcutRegistryEntry } from "./logic";

export async function fetchShortcutRegistry(): Promise<{ results: ShortcutRegistryEntry[] }> {
  return request("/api/v1/meta/shortcuts/");
}

export async function fetchShortcutPreferences(): Promise<{ effective: ShortcutBindings; defaults: ShortcutBindings }> {
  return request("/api/v1/users/me/preferences/shortcuts/");
}

export async function saveShortcutPreferences(values: ShortcutBindings): Promise<{ effective: ShortcutBindings }> {
  return request("/api/v1/users/me/preferences/shortcuts/", {
    method: "PUT",
    body: JSON.stringify({ values }),
  });
}
