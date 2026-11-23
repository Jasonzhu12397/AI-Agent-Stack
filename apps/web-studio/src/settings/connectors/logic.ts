/** Only secret references are accepted; plaintext secrets are never sent or echoed back. */
export const CREDENTIAL_REF_RE = /^(env|vault):\/\/[^\s]+$/;

export type InstallationRecord = {
  id: string;
  slug: string;
  scope: string;
  status: string;
  configured: boolean;
  masked_hint: string | null;
  credential_ref: string | null;
};

export type ConnectorEntry = {
  slug: string;
  name: string;
  description: string;
  connector_type: string;
  scope: string;
  installed: boolean;
  health: string;
  installation: InstallationRecord | null;
};

export type TestOutcome = {
  status: "success" | "failed";
  summary: string;
  code: string;
};

/** Validates a credential reference. Plaintext tokens, URLs and unknown schemes are rejected client-side. */
export function validateCredentialRef(ref: string): { ok: boolean; code: string } {
  const trimmed = ref.trim();
  if (!trimmed) return { ok: false, code: "CREDENTIAL_REF_REQUIRED" };
  if (CREDENTIAL_REF_RE.test(trimmed)) return { ok: true, code: "" };
  return { ok: false, code: "CREDENTIAL_REF_INVALID" };
}

/** Maps a raw health status to a display label; anything unknown renders as Unknown. */
export function healthLabel(health: string): string {
  switch (health) {
    case "healthy":
      return "Healthy";
    case "unhealthy":
      return "Unhealthy";
    case "disabled":
      return "Disabled";
    default:
      return "Unknown";
  }
}

export function healthTagType(health: string): "success" | "danger" | "info" | "warning" {
  switch (health) {
    case "healthy":
      return "success";
    case "unhealthy":
      return "danger";
    case "disabled":
      return "info";
    default:
      return "warning";
  }
}

export function scopeLabel(scope: string): string {
  switch (scope) {
    case "tenant":
      return "Tenant";
    case "project":
      return "Project";
    case "global":
      return "Global";
    default:
      return scope || "Unknown";
  }
}

/**
 * Deterministic idempotency key for an install request. Replaying the same
 * install for a connector produces the same key, so the server treats it as a no-op.
 */
export function installationIdempotencyKey(slug: string): string {
  const safe = slug.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
  return `connector-install:${safe}`;
}

/**
 * Test invocation state machine. A failed call keeps its server code and must
 * never be rendered as connected/healthy; only a payload with `ok: true` is success.
 */
export function summarizeTestResult(payload: unknown): TestOutcome {
  if (!payload || typeof payload !== "object") {
    return { status: "failed", summary: "Test invocation failed.", code: "TEST_FAILED" };
  }
  const p = payload as Record<string, unknown>;
  const code = typeof p.code === "string" && p.code ? p.code : "TEST_FAILED";
  const summary =
    typeof p.summary === "string" && p.summary
      ? p.summary
      : typeof p.message === "string" && p.message
        ? p.message
        : "";
  if (p.ok === true) {
    return { status: "success", summary: summary || "Test invocation succeeded.", code: "" };
  }
  return {
    status: "failed",
    summary: summary || `Test invocation failed (${code}).`,
    code,
  };
}

/** Four-state mapping for load errors: 401/403 are denied, everything else is a load error. */
export function resolveLoadState(status: number): "denied" | "error" {
  return status === 401 || status === 403 ? "denied" : "error";
}

export function describeError(code: string): string {
  switch (code) {
    case "CREDENTIAL_REF_INVALID":
      return "Only env:// or vault:// secret references are accepted; plaintext secrets are not sent.";
    case "CREDENTIAL_REF_REQUIRED":
      return "A secret reference is required for this connector.";
    case "CONNECTOR_NOT_FOUND":
      return "This connector is no longer available in the catalog.";
    case "INSTALLATION_NOT_FOUND":
      return "This installation no longer exists.";
    case "HTTP_401":
      return "Your session has expired. Sign in again.";
    case "HTTP_403":
      return "Only tenant administrators can install or reconfigure connectors.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export const pageLabels = [
  "Plugins & Connectors",
  "Connector catalog",
  "Installed",
  "Health",
  "Scope",
  "Install",
  "Credential reference",
  "Configured",
  "Masked hint",
  "Health check",
  "Test connection",
  "Connection test succeeded",
  "Connection test failed",
  "No connectors are available for this tenant yet.",
  "Only tenant administrators can install or reconfigure connectors.",
  "Tenant",
  "Project",
  "Global",
  "Connector type",
  "Save configuration",
  "Secret values are never sent to or displayed by the browser; only an env:// or vault:// reference is accepted.",
  "Reinstall",
  "Yes",
  "No",
  "env://… or vault://…",
];
