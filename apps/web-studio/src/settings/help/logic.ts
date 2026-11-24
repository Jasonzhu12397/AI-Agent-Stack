export const FEEDBACK_CATEGORIES = ["bug", "feature_request", "question", "other"] as const;
export const FEEDBACK_SEVERITIES = ["low", "medium", "high"] as const;

export type FeedbackCategory = (typeof FEEDBACK_CATEGORIES)[number];
export type FeedbackSeverity = (typeof FEEDBACK_SEVERITIES)[number];

export type FeedbackRecord = {
  id: string;
  category: string;
  severity: string;
  body: string;
  created_at: string | null;
};

export type FeedbackFormInput = {
  category: string;
  severity: string;
  body: string;
};

export type ValidationResult = {
  ok: boolean;
  code: string;
  fieldErrors: Record<string, string>;
};

const MIN_BODY_LENGTH = 10;

/** Curated help resources. Every entry must be a real, reachable absolute URL; no anchors or placeholders. */
export const HELP_LINKS: { label: string; href: string }[] = [
  {
    label: "Source code repository",
    href: "https://github.com/Jasonzhu12397/AI-Agent-Stack",
  },
  {
    label: "Platform documentation",
    href: "https://github.com/Jasonzhu12397/AI-Agent-Stack/tree/main/docs",
  },
  {
    label: "Settings center contract",
    href: "https://github.com/Jasonzhu12397/AI-Agent-Stack/blob/main/docs/settings-center-contract.md",
  },
  {
    label: "Verification matrix",
    href: "https://github.com/Jasonzhu12397/AI-Agent-Stack/blob/main/docs/verification-matrix.md",
  },
];

/** Client-side validation; the server re-checks and returns field details on 400. */
export function validateFeedbackForm(input: FeedbackFormInput): ValidationResult {
  const fieldErrors: Record<string, string> = {};
  const category = input.category.trim();
  const severity = input.severity.trim();
  const body = input.body.trim();

  if (!(FEEDBACK_CATEGORIES as readonly string[]).includes(category)) {
    fieldErrors.category = "FEEDBACK_INVALID_CATEGORY";
  }
  if (!(FEEDBACK_SEVERITIES as readonly string[]).includes(severity)) {
    fieldErrors.severity = "FEEDBACK_INVALID_SEVERITY";
  }
  if (body.length < MIN_BODY_LENGTH) {
    fieldErrors.body = "FEEDBACK_EMPTY_BODY";
  }

  if (Object.keys(fieldErrors).length > 0) {
    const code = fieldErrors.category ?? fieldErrors.severity ?? fieldErrors.body ?? "FEEDBACK_INVALID";
    return { ok: false, code, fieldErrors };
  }
  return { ok: true, code: "", fieldErrors: {} };
}

/** Normalizes the server's `details` envelope into a flat `{field: reason}` map. */
export function fieldErrorsFromDetails(
  details: Record<string, string[] | string> | undefined,
): Record<string, string> {
  if (!details || typeof details !== "object") return {};
  const result: Record<string, string> = {};
  for (const [field, value] of Object.entries(details)) {
    result[field] = Array.isArray(value) ? value.join("; ") : String(value);
  }
  return result;
}

export function describeError(code: string): string {
  switch (code) {
    case "FEEDBACK_RATE_LIMITED":
      return "You hit the feedback rate limit. Wait a moment and try again.";
    case "FEEDBACK_INVALID_CATEGORY":
      return "Choose a valid feedback category.";
    case "FEEDBACK_INVALID_SEVERITY":
      return "Choose a valid severity level.";
    case "FEEDBACK_EMPTY_BODY":
      return "Describe the issue in at least a few words.";
    case "HTTP_401":
      return "Your session has expired. Sign in again.";
    case "HTTP_403":
      return "Only tenant members can submit feedback.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export function categoryLabel(category: string): string {
  switch (category) {
    case "bug":
      return "Bug report";
    case "feature_request":
      return "Feature request";
    case "question":
      return "Question";
    default:
      return "Other";
  }
}

export function severityLabel(severity: string): string {
  switch (severity) {
    case "low":
      return "Low";
    case "medium":
      return "Medium";
    case "high":
      return "High";
    default:
      return severity || "unknown";
  }
}

export const pageLabels = [
  "Help & Feedback",
  "Send feedback",
  "Category",
  "Severity",
  "Message",
  "Submit feedback",
  "Feedback submitted",
  "Help resources",
  "Bug report",
  "Feature request",
  "Question",
  "Other",
  "Low",
  "Medium",
  "High",
  "Previous feedback",
  "No feedback has been submitted yet.",
  "Only tenant members can submit feedback.",
  "Reference",
  "Describe the issue or your request.",
];
