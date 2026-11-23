import { request, tenantPath } from "../../services/http";
import type { DataControlsEnvelope, DataControlsValues, ExportJob } from "./logic";

export async function fetchDataControls(tenantId: string): Promise<DataControlsEnvelope> {
  return request(tenantPath(tenantId, "/settings/data-controls/"));
}

export async function saveDataControls(
  tenantId: string,
  values: Partial<DataControlsValues>,
): Promise<DataControlsEnvelope> {
  return request(tenantPath(tenantId, "/settings/data-controls/"), {
    method: "PATCH",
    body: JSON.stringify({ values }),
  });
}

export async function fetchExportJobs(tenantId: string): Promise<ExportJob[]> {
  const page = await request<{ results: ExportJob[] }>(tenantPath(tenantId, "/data-controls/exports/"));
  return page.results ?? [];
}

export async function createExportJob(
  tenantId: string,
  payload: { export_format: string; confirm: boolean; idempotency_key: string },
): Promise<ExportJob> {
  return request(tenantPath(tenantId, "/data-controls/exports/"), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function expireExportJob(tenantId: string, jobId: string): Promise<ExportJob> {
  return request(tenantPath(tenantId, `/data-controls/exports/${jobId}/expire/`), { method: "POST" });
}
