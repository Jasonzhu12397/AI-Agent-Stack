import { request, tenantPath } from "../../services/http";
import {
  mapPrivacySessions,
  type DeletionRequest,
  type Grant,
  type PrivacySession,
  type PrivacySessionRow,
} from "./logic";

export async function fetchGrants(tenantId: string): Promise<Grant[]> {
  const page = await request<{ results: Grant[] }>(tenantPath(tenantId, "/privacy/grants/"));
  return page.results ?? [];
}

export async function revokeGrant(tenantId: string, grantId: string): Promise<void> {
  return request(tenantPath(tenantId, `/privacy/grants/${grantId}/`), { method: "DELETE" });
}

export async function fetchDeletionRequests(tenantId: string): Promise<DeletionRequest[]> {
  const page = await request<{ results: DeletionRequest[] }>(
    tenantPath(tenantId, "/privacy/deletion-requests/"),
  );
  return page.results ?? [];
}

export async function createDeletionRequest(
  tenantId: string,
  payload: { scope: string; confirm: boolean; idempotency_key: string },
): Promise<DeletionRequest> {
  return request(tenantPath(tenantId, "/privacy/deletion-requests/"), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function fetchSessions(tenantId: string): Promise<PrivacySession[]> {
  const page = await request<{ results: PrivacySessionRow[] }>(
    tenantPath(tenantId, "/privacy/sessions/"),
  );
  return mapPrivacySessions(page.results ?? []);
}
