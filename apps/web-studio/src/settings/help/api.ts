import { request, tenantPath } from "../../services/http";
import type { FeedbackFormInput, FeedbackRecord } from "./logic";

export type FeedbackSubmission = {
  id: string;
  created_at: string | null;
  status?: string;
};

export async function fetchFeedback(tenantId: string): Promise<FeedbackRecord[]> {
  const page = await request<{ results: FeedbackRecord[] }>(tenantPath(tenantId, "/feedback/"));
  return page.results ?? [];
}

export async function submitFeedback(
  tenantId: string,
  input: FeedbackFormInput,
): Promise<FeedbackSubmission> {
  return request(tenantPath(tenantId, "/feedback/"), {
    method: "POST",
    body: JSON.stringify({ category: input.category, severity: input.severity, body: input.body }),
  });
}
