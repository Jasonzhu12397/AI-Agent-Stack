import { request, tenantPath } from "../../services/http";
import type { ConnectorEntry, InstallationRecord } from "./logic";

export async function fetchConnectors(tenantId: string): Promise<ConnectorEntry[]> {
  const page = await request<{ results: ConnectorEntry[] }>(tenantPath(tenantId, "/connectors/"));
  return page.results ?? [];
}

export async function installConnector(
  tenantId: string,
  slug: string,
  idempotencyKey: string,
): Promise<InstallationRecord> {
  return request(tenantPath(tenantId, `/connectors/${encodeURIComponent(slug)}/install/`), {
    method: "POST",
    body: JSON.stringify({ idempotency_key: idempotencyKey }),
  });
}

export type InstallationPatch = {
  display_name?: string;
  credential_ref?: string;
};

export async function updateInstallation(
  tenantId: string,
  installationId: string,
  patch: InstallationPatch,
): Promise<InstallationRecord> {
  return request(tenantPath(tenantId, `/installations/${encodeURIComponent(installationId)}/`), {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export async function runHealthCheck(
  tenantId: string,
  installationId: string,
): Promise<{ health?: string }> {
  return request(tenantPath(tenantId, `/installations/${encodeURIComponent(installationId)}/health/`), {
    method: "POST",
  });
}

export async function testInstallation(
  tenantId: string,
  installationId: string,
): Promise<Record<string, unknown>> {
  return request(tenantPath(tenantId, `/installations/${encodeURIComponent(installationId)}/test/`), {
    method: "POST",
  });
}
