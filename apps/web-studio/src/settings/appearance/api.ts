import { request } from "../../services/http";
import type { AppearanceSettings } from "./logic";

export async function fetchAppearance(): Promise<{ values: AppearanceSettings; defaults: AppearanceSettings }> {
  return request("/api/v1/users/me/preferences/appearance/");
}

export async function saveAppearance(values: AppearanceSettings): Promise<{ values: AppearanceSettings }> {
  return request("/api/v1/users/me/preferences/appearance/", {
    method: "PUT",
    body: JSON.stringify({ values }),
  });
}
