export const DELETION_SCOPES = ["archived_content", "exports"] as const;
export type DeletionScope = (typeof DELETION_SCOPES)[number];

export const PRIVACY_DEPENDENCY_UNAVAILABLE = "PRIVACY_DEPENDENCY_UNAVAILABLE";

export type Grant = {
  id: string;
  subject: string;
  permission: string;
  scope: string;
  granted_at: string | null;
  actor: string | null;
};

export type DeletionRequest = {
  id: string;
  scope: string;
  status: string;
  audit_reference: string | null;
  confirm: boolean;
  created_at: string | null;
};

export type PrivacySession = {
  id: string;
  actor: string | null;
  created_at: string | null;
  last_seen_at: string | null;
};

export type PrivacyBundle = {
  grants: Grant[];
  deletionRequests: DeletionRequest[];
  sessions: PrivacySession[] | null;
  sessionsAvailable: boolean;
  sessionsReason: string;
};

export function isValidScope(scope: string): scope is DeletionScope {
  return (DELETION_SCOPES as readonly string[]).includes(scope);
}

/**
 * Deletion-request gate: scope must be one of the fixed enum, the request must be explicitly
 * confirmed, and a non-empty idempotency key is required so replays collapse server-side.
 */
export function deletionRequestGate(input: {
  scope: string;
  confirm: boolean;
  idempotencyKey: string;
}): { ok: boolean; missing: string[] } {
  const missing: string[] = [];
  if (!isValidScope(input.scope)) missing.push("scope");
  if (!input.confirm) missing.push("confirm");
  if (!input.idempotencyKey.trim()) missing.push("idempotency_key");
  return { ok: missing.length === 0, missing };
}

export function deletionScopeLabel(scope: string): string {
  switch (scope) {
    case "archived_content":
      return "Archived content";
    case "exports":
      return "Data exports";
    default:
      return "Unknown scope";
  }
}

export function deletionStatusMeta(status: string): {
  label: string;
  tagType: "info" | "warning" | "success" | "danger";
} {
  switch (status) {
    case "pending":
      return { label: "Pending review", tagType: "info" };
    case "approved":
      return { label: "Approved", tagType: "warning" };
    case "completed":
      return { label: "Completed", tagType: "success" };
    case "rejected":
      return { label: "Rejected", tagType: "danger" };
    case "failed":
      return { label: "Failed", tagType: "danger" };
    default:
      return { label: "Unknown", tagType: "info" };
  }
}

/** Grant revocation gate: explicit confirmation; the action is recorded in the audit log. */
export function revokeGrantGate(confirmed: boolean): { ok: boolean; reason: string } {
  return confirmed
    ? { ok: true, reason: "" }
    : { ok: false, reason: "Revoking this permission removes the grant immediately and is recorded in the audit log." };
}

/**
 * Session availability: a 503 (or the explicit PRIVACY_DEPENDENCY_UNAVAILABLE code) means the
 * session dependency is down. The client must show an explicit unavailable state and must never
 * fabricate a session list.
 */
export function sessionAvailability(input: {
  status: number;
  code: string;
}): { available: boolean; reasonCode: string } {
  if (input.status === 503 || input.code === PRIVACY_DEPENDENCY_UNAVAILABLE) {
    return { available: false, reasonCode: input.code || "HTTP_503" };
  }
  return { available: true, reasonCode: "" };
}

export function describeError(code: string): string {
  switch (code) {
    case PRIVACY_DEPENDENCY_UNAVAILABLE:
      return "The session or privacy dependency is currently unavailable; session data cannot be listed.";
    case "PRIVACY_GRANT_NOT_FOUND":
      return "This grant no longer exists on the server.";
    case "PRIVACY_DELETION_SCOPE_INVALID":
      return "The deletion scope is not supported.";
    case "PRIVACY_DELETION_NOT_CONFIRMED":
      return "The deletion request must be explicitly confirmed.";
    case "HTTP_401":
      return "Your session has expired. Sign in again.";
    case "HTTP_403":
      return "Only tenant administrators can revoke grants or submit deletion requests.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export const pageLabels = [
  "Privacy & Permissions",
  "Active grants",
  "Subject",
  "Permission",
  "Scope",
  "Granted at",
  "Revoke grant",
  "Revoke",
  "Deletion requests",
  "Request deletion",
  "Deletion scope",
  "Archived content",
  "Data exports",
  "Status",
  "Audit reference",
  "Pending review",
  "Approved",
  "Completed",
  "Rejected",
  "Active sessions",
  "No grants recorded",
  "No deletion requests yet",
  "Sessions unavailable",
];
