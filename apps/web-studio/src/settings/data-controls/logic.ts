export type ExportFormat = "json" | "csv" | "xlsx";

export type DataControlsValues = {
  retention_days: number;
  export_format: ExportFormat;
  allow_export: boolean;
};

export type DataControlsEnvelope = {
  section: string;
  scope: string;
  values: Partial<DataControlsValues>;
  effective: DataControlsValues;
  defaults: DataControlsValues;
  updated_at: string | null;
};

export type ExportJobStatus = "pending" | "running" | "succeeded" | "failed" | "expired";

export type ExportJob = {
  id: string;
  status: ExportJobStatus | string;
  row_count: number | null;
  download_url: string | null;
  export_format: string | null;
  created_at: string | null;
  expires_at: string | null;
  error_code: string | null;
};

export const DATA_CONTROLS_DEFAULTS: DataControlsValues = {
  retention_days: 90,
  export_format: "json",
  allow_export: true,
};

export const EXPORT_FORMATS: ExportFormat[] = ["json", "csv", "xlsx"];

export const EXPORT_JOB_STATUSES: ExportJobStatus[] = [
  "pending",
  "running",
  "succeeded",
  "failed",
  "expired",
];

/** Deterministic prefix for export idempotency keys; the one-shot suffix is minted per click. */
export const EXPORT_IDEMPOTENCY_PREFIX = "dc-export";

/** Coerces partial server values onto the frozen defaults; the UI never invents its own defaults. */
export function normalizeDataControls(raw: unknown): DataControlsValues {
  const result: DataControlsValues = { ...DATA_CONTROLS_DEFAULTS };
  if (!raw || typeof raw !== "object") return result;
  const value = raw as Record<string, unknown>;
  if (
    typeof value.retention_days === "number" &&
    Number.isInteger(value.retention_days) &&
    value.retention_days >= 0
  ) {
    result.retention_days = value.retention_days;
  }
  if (typeof value.export_format === "string" && (EXPORT_FORMATS as string[]).includes(value.export_format)) {
    result.export_format = value.export_format as ExportFormat;
  }
  if (typeof value.allow_export === "boolean") result.allow_export = value.allow_export;
  return result;
}

/**
 * Builds an idempotency key: a stable resource prefix plus a one-shot suffix minted for the
 * specific click, so replays of the same request collapse server-side but two real attempts differ.
 */
export function buildIdempotencyKey(prefix: string, suffix: string): string {
  const cleanPrefix = prefix.trim();
  const cleanSuffix = suffix.trim();
  if (!cleanPrefix || !cleanSuffix) return "";
  return `${cleanPrefix}:${cleanSuffix}`;
}

/** Confirmation gate for a data export: explicit acknowledgement AND a non-empty idempotency key. */
export function exportGate(input: {
  confirmed: boolean;
  idempotencyKey: string;
}): { ok: boolean; missing: string[] } {
  const missing: string[] = [];
  if (!input.confirmed) missing.push("confirm");
  if (!input.idempotencyKey.trim()) missing.push("idempotency_key");
  return { ok: missing.length === 0, missing };
}

/**
 * A download is offered only when the job has actually succeeded and the server returned a URL.
 * Pending/running/failed/expired jobs never expose any download content or link on the client.
 */
export function canDownloadExport(job: Pick<ExportJob, "status" | "download_url">): boolean {
  return job.status === "succeeded" && typeof job.download_url === "string" && job.download_url.trim() !== "";
}

export function exportStatusMeta(status: string): {
  label: string;
  tagType: "info" | "warning" | "success" | "danger";
} {
  switch (status) {
    case "pending":
      return { label: "Queued", tagType: "info" };
    case "running":
      return { label: "Preparing export", tagType: "warning" };
    case "succeeded":
      return { label: "Ready for download", tagType: "success" };
    case "failed":
      return { label: "Failed", tagType: "danger" };
    case "expired":
      return { label: "Expired", tagType: "info" };
    default:
      return { label: "Unknown", tagType: "info" };
  }
}

/**
 * Confirmation copy must state the export scope, the retention period the data is held under,
 * and that the operation is irreversible.
 */
export function exportScopeWarning(retentionDays: number): string {
  const days =
    Number.isFinite(retentionDays) && retentionDays >= 0
      ? Math.floor(retentionDays)
      : DATA_CONTROLS_DEFAULTS.retention_days;
  return `This export covers all tenant data retained under the current ${days}-day retention period. ` +
    `The request is irreversible: once the export job expires or the source data is purged, it cannot be restored.`;
}

export function describeError(code: string): string {
  switch (code) {
    case "SETTINGS_UNKNOWN_KEY":
      return "This data-control setting no longer exists on the server.";
    case "SETTINGS_INVALID_VALUE":
      return "The server rejected a value; check the highlighted field.";
    case "EXPORT_NOT_ALLOWED":
      return "Tenant administrators have disabled data exports for this tenant.";
    case "EXPORT_JOB_EXPIRED":
      return "This export job has expired and can no longer be downloaded.";
    case "HTTP_401":
      return "Your session has expired. Sign in again.";
    case "HTTP_403":
      return "Only tenant administrators can change data controls or request exports.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export const pageLabels = [
  "Data Controls",
  "Retention & export policy",
  "Retention period (days)",
  "Export format",
  "Allow data export",
  "Defaults",
  "Effective value",
  "Last updated",
  "Export jobs",
  "Request export",
  "Queued",
  "Preparing export",
  "Ready for download",
  "Failed",
  "Expired",
  "Row count",
  "Download",
  "Expire export",
  "Confirm export",
  "Export scope",
  "No export jobs yet",
];
