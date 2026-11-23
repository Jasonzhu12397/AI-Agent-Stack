export type NotificationChannel = "in_app" | "email" | "webhook";
export type NotificationCategory =
  | "run_failed"
  | "run_succeeded"
  | "approval_required"
  | "budget_threshold"
  | "connector_health";

export const CHANNELS: NotificationChannel[] = ["in_app", "email", "webhook"];
export const CATEGORIES: NotificationCategory[] = [
  "run_failed",
  "run_succeeded",
  "approval_required",
  "budget_threshold",
  "connector_health",
];

export type NotificationMatrix = Record<NotificationCategory, Record<NotificationChannel, boolean>>;

export const NOTIFICATION_DEFAULTS: { matrix: NotificationMatrix; digest_frequency: "immediate" | "daily" | "weekly" } = {
  matrix: {
    run_failed: { in_app: true, email: false, webhook: false },
    run_succeeded: { in_app: false, email: false, webhook: false },
    approval_required: { in_app: true, email: true, webhook: false },
    budget_threshold: { in_app: true, email: true, webhook: false },
    connector_health: { in_app: true, email: false, webhook: false },
  },
  digest_frequency: "immediate",
};

export function emptyMatrix(): NotificationMatrix {
  const matrix = {} as NotificationMatrix;
  for (const category of CATEGORIES) {
    matrix[category] = { in_app: false, email: false, webhook: false };
  }
  return matrix;
}

/** Builds a complete matrix from partial server values, defaulting missing cells to false. */
export function normalizeMatrix(raw: unknown): NotificationMatrix {
  const matrix = emptyMatrix();
  if (!raw || typeof raw !== "object") return matrix;
  const value = raw as Record<string, Record<string, unknown> | undefined>;
  for (const category of CATEGORIES) {
    const row = value[category];
    if (!row || typeof row !== "object") continue;
    for (const channel of CHANNELS) {
      matrix[category][channel] = row[channel] === true;
    }
  }
  return matrix;
}

/** User delivery preferences can only narrow the tenant matrix, never silently widen it. */
export function foldDelivery(
  tenantMatrix: NotificationMatrix,
  userDelivery: Partial<Record<NotificationChannel, boolean>>,
): NotificationMatrix {
  const folded = normalizeMatrix(tenantMatrix);
  for (const category of CATEGORIES) {
    for (const channel of CHANNELS) {
      if (userDelivery[channel] === false) folded[category][channel] = false;
    }
  }
  return folded;
}

export function toggleChannel(
  matrix: NotificationMatrix,
  category: NotificationCategory,
  channel: NotificationChannel,
): NotificationMatrix {
  const next = normalizeMatrix(matrix);
  next[category][channel] = !next[category][channel];
  return next;
}

export function digestLabel(frequency: string): string {
  switch (frequency) {
    case "daily":
      return "Daily digest";
    case "weekly":
      return "Weekly digest";
    default:
      return "Immediate delivery";
  }
}

export function describeError(code: string): string {
  switch (code) {
    case "SETTINGS_UNKNOWN_KEY":
      return "This notification setting no longer exists on the server.";
    case "SETTINGS_INVALID_VALUE":
      return "The server rejected a value; check the highlighted field.";
    case "HTTP_403":
      return "Only tenant administrators can change tenant notification rules.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export const pageLabels = [
  "Notification Settings",
  "Event categories",
  "Delivery channels",
  "In-app",
  "Email",
  "Webhook",
  "Digest frequency",
  "User delivery preferences",
  "Tenant notification rules",
  "Run failed",
  "Run succeeded",
  "Approval required",
  "Budget threshold",
  "Connector health",
];
