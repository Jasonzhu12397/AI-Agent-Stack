export type ArchiveItem = {
  id: string;
  type: string;
  name: string;
  description: string | null;
  archived_at: string | null;
  status: "archived" | "restored" | string;
};

export type ArchivePage = { count: number; results: ArchiveItem[] };

export type ArchiveQuery = { type: string; q: string; page: number };

export const ARCHIVE_ITEM_TYPES = ["agent", "agent_team", "workflow", "knowledge_base"] as const;

/**
 * Builds the query string parameters for the archive listing. Empty filters are omitted so the
 * server never receives blank type/q values; page always defaults to 1.
 */
export function buildArchiveQuery(filters: Partial<ArchiveQuery>): Record<string, string> {
  const params: Record<string, string> = {};
  const type = (filters.type ?? "").trim();
  const q = (filters.q ?? "").trim();
  const rawPage = filters.page ?? 1;
  const page = Number.isFinite(rawPage) && rawPage > 0 ? Math.floor(rawPage) : 1;
  if (type) params.type = type;
  if (q) params.q = q;
  params.page = String(page);
  return params;
}

/** Restore is idempotent: an already-restored item reports `already_restored` instead of failing. */
export function isAlreadyRestored(status: string): boolean {
  return status === "already_restored" || status === "restored";
}

export function restoreResultMeta(status: string): { label: string; alreadyRestored: boolean } {
  if (isAlreadyRestored(status)) {
    return { label: "Already restored", alreadyRestored: true };
  }
  return { label: "Restored", alreadyRestored: false };
}

/**
 * Permanent deletion gate: the action must be explicitly confirmed. The reason copy states the
 * deletion is unrecoverable; there is no unconfirmed bulk delete on this page.
 */
export function deleteGate(confirmed: boolean): { ok: boolean; reason: string } {
  return confirmed
    ? { ok: true, reason: "" }
    : {
        ok: false,
        reason: "This permanently deletes the item; the action is unrecoverable and cannot be undone.",
      };
}

export function describeError(code: string): string {
  switch (code) {
    case "ARCHIVE_ITEM_NOT_FOUND":
      return "This archived item no longer exists on the server.";
    case "ARCHIVE_PERMANENT_DELETE_DENIED":
      return "Your permissions do not allow permanent deletion of archived items.";
    case "ARCHIVE_RESTORE_DENIED":
      return "Your role does not allow restoring archived items.";
    case "HTTP_401":
      return "Your session has expired. Sign in again.";
    case "HTTP_403":
      return "Only project administrators can restore or permanently delete archived content.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export const pageLabels = [
  "Archived Content",
  "Search archived items",
  "Item type",
  "All types",
  "Name",
  "Description",
  "Archived at",
  "Status",
  "Restore",
  "Already restored",
  "Permanently delete",
  "Delete archived item",
  "This permanently deletes the item; the action is unrecoverable and cannot be undone.",
  "No archived items match the current filters.",
  "Archived",
  "Restored",
];
