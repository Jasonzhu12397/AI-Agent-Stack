import { projectPath, request } from "../../services/http";
import { buildArchiveQuery, type ArchiveItem, type ArchivePage, type ArchiveQuery } from "./logic";

export async function fetchArchiveItems(
  tenantId: string,
  projectId: string,
  query: Partial<ArchiveQuery>,
): Promise<ArchivePage> {
  const params = new URLSearchParams(buildArchiveQuery(query)).toString();
  const suffix = `/archive-items/${params ? `?${params}` : ""}`;
  return request(projectPath(tenantId, projectId, suffix));
}

export async function restoreArchiveItem(
  tenantId: string,
  projectId: string,
  type: string,
  id: string,
): Promise<{ status: string }> {
  return request(projectPath(tenantId, projectId, `/archive-items/${type}/${id}/restore/`), {
    method: "POST",
  });
}

export async function deleteArchiveItem(
  tenantId: string,
  projectId: string,
  type: string,
  id: string,
): Promise<void> {
  return request(projectPath(tenantId, projectId, `/archive-items/${type}/${id}/`), { method: "DELETE" });
}

export type { ArchiveItem };
