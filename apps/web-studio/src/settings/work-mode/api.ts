import { projectPath, request, tenantPath } from "../../services/http";
import type { WorkModeValues } from "./logic";

export type SectionEnvelope<T> = {
  values: T;
  defaults: T;
  effective: T;
  sources: Record<string, string>;
  updated_at: string | null;
};

export function fetchWorkMode(tenantId: string, projectId: string | null): Promise<SectionEnvelope<WorkModeValues>> {
  const path = projectId
    ? projectPath(tenantId, projectId, "/settings/work-mode/")
    : tenantPath(tenantId, "/settings/work-mode/");
  return request(path);
}

export function saveWorkMode(
  tenantId: string,
  projectId: string | null,
  values: WorkModeValues,
): Promise<SectionEnvelope<WorkModeValues>> {
  const path = projectId
    ? projectPath(tenantId, projectId, "/settings/work-mode/")
    : tenantPath(tenantId, "/settings/work-mode/");
  return request(path, { method: "PATCH", body: JSON.stringify({ values }) });
}
