export type WorkModeValues = {
  default_execution_mode: "review" | "auto";
  require_confirmation: boolean;
  auto_continue: boolean;
  max_concurrency: number;
  workspace_default_branch: string;
};

export const WORK_MODE_DEFAULTS: WorkModeValues = {
  default_execution_mode: "review",
  require_confirmation: true,
  auto_continue: false,
  max_concurrency: 4,
  workspace_default_branch: "main",
};

export const MAX_CONCURRENCY_LIMIT = 32;
const BRANCH_PATTERN = /^[A-Za-z0-9._\-/]+$/;

export function normalizeWorkMode(raw: unknown): WorkModeValues {
  const result = { ...WORK_MODE_DEFAULTS };
  if (!raw || typeof raw !== "object") return result;
  const value = raw as Record<string, unknown>;
  if (value.default_execution_mode === "auto" || value.default_execution_mode === "review") {
    result.default_execution_mode = value.default_execution_mode;
  }
  if (typeof value.require_confirmation === "boolean") result.require_confirmation = value.require_confirmation;
  if (typeof value.auto_continue === "boolean") result.auto_continue = value.auto_continue;
  if (typeof value.max_concurrency === "number" && Number.isInteger(value.max_concurrency)) {
    result.max_concurrency = Math.min(MAX_CONCURRENCY_LIMIT, Math.max(1, value.max_concurrency));
  }
  if (typeof value.workspace_default_branch === "string" && BRANCH_PATTERN.test(value.workspace_default_branch)) {
    result.workspace_default_branch = value.workspace_default_branch;
  }
  return result;
}

export function validateWorkMode(values: WorkModeValues): { ok: boolean; errors: Record<string, string> } {
  const errors: Record<string, string> = {};
  if (!Number.isInteger(values.max_concurrency) || values.max_concurrency < 1 || values.max_concurrency > MAX_CONCURRENCY_LIMIT) {
    errors.max_concurrency = "WORK_MODE_INVALID_CONCURRENCY";
  }
  if (!BRANCH_PATTERN.test(values.workspace_default_branch)) {
    errors.workspace_default_branch = "WORK_MODE_INVALID_BRANCH";
  }
  if (values.require_confirmation === false && values.default_execution_mode === "auto") {
    errors.require_confirmation = "WORK_MODE_UNSAFE_AUTONOMY";
  }
  return { ok: Object.keys(errors).length === 0, errors };
}

export function describeError(code: string): string {
  switch (code) {
    case "WORK_MODE_INVALID_CONCURRENCY":
      return `Concurrency must be an integer between 1 and ${MAX_CONCURRENCY_LIMIT}.`;
    case "WORK_MODE_INVALID_BRANCH":
      return "Branch names may contain letters, digits, dot, dash, slash and underscore only.";
    case "WORK_MODE_UNSAFE_AUTONOMY":
      return "Automatic execution without confirmation is not allowed.";
    case "HTTP_403":
      return "Only project administrators can change project work-mode settings.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export function sourceLabel(layer: string): string {
  switch (layer) {
    case "project":
      return "Project override";
    case "tenant":
      return "Tenant setting";
    case "user":
      return "User preference";
    default:
      return "Default";
  }
}

export const pageLabels = [
  "Work Mode Settings",
  "Execution preferences",
  "Default execution mode",
  "Require confirmation",
  "Auto continue",
  "Max concurrency",
  "Workspace default branch",
  "Scope",
  "Tenant scope",
  "Project scope",
];
