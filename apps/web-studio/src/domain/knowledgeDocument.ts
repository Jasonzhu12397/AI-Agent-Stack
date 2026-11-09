export type KnowledgeDocumentStatus = "pending" | "ingesting" | "ready" | "failed";

export type KnowledgeDocumentStatusPresentation = {
  label: string;
  type: "info" | "warning" | "success" | "danger";
};

const statusPresentations: Record<KnowledgeDocumentStatus, KnowledgeDocumentStatusPresentation> = {
  pending: { label: "Queued", type: "info" },
  ingesting: { label: "Indexing", type: "warning" },
  ready: { label: "Ready", type: "success" },
  failed: { label: "Failed", type: "danger" },
};

export function getKnowledgeDocumentStatus(status: string): KnowledgeDocumentStatusPresentation {
  if (Object.hasOwn(statusPresentations, status)) {
    return statusPresentations[status as KnowledgeDocumentStatus];
  }
  return { label: "Unknown", type: "info" };
}

export function getKnowledgeDocumentError(error: Record<string, unknown> | null): string {
  if (!error) return "";
  const message = error.message;
  if (typeof message === "string" && message.trim()) return message.trim();
  const code = error.code;
  if (typeof code === "string" && code.trim()) return code.trim();
  return "";
}
