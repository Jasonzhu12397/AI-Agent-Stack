export type UsageSnapshot = {
  daily_limit: number;
  monthly_limit: number;
  daily_used: number;
  monthly_used: number;
  period_start: string | null;
  period_end: string | null;
  history: { period: string; consumed_tokens: number; source: string }[];
};

export type OrderRecord = {
  id: string;
  plan_code: string;
  status: "created" | "pending" | "paid" | "failed" | "closed" | "refunded";
  amount_cents: number;
  currency: string;
  channel: "wechat" | "alipay";
  created_at: string;
  paid_at: string | null;
};

export type SubscriptionBundle = {
  subscription: {
    plan_name: string;
    status: string;
    current_period_start: string | null;
    current_period_end: string | null;
    cancel_at_period_end: boolean;
    renewal_mode: string;
  } | null;
  usage: UsageSnapshot;
  orders: OrderRecord[];
};

export function orderStatusLabel(status: OrderRecord["status"]): string {
  switch (status) {
    case "created":
      return "Created · unpaid";
    case "pending":
      return "Awaiting payment";
    case "paid":
      return "Paid";
    case "failed":
      return "Payment failed";
    case "closed":
      return "Closed";
    case "refunded":
      return "Refunded";
    default:
      return "Unknown";
  }
}

/**
 * Only the server's `paid` status means paid. Anything else, including a stale pending order,
 * stays visibly unpaid — the client never infers payment from an order existing.
 */
export function isPaid(order: Pick<OrderRecord, "status">): boolean {
  return order.status === "paid";
}

export function usagePercent(used: number, limit: number): number {
  if (!Number.isFinite(used) || used < 0) return 0;
  if (!Number.isFinite(limit) || limit <= 0) return 0;
  const ratio = (used / limit) * 100;
  return Math.min(100, Math.max(0, Math.round(ratio * 10) / 10));
}

export function formatTokens(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens < 0) return "—";
  return tokens.toLocaleString("en-US");
}

export function formatAmount(cents: number, currency: string): string {
  if (!Number.isFinite(cents) || cents < 0) return `${currency} —`;
  return `${currency === "CNY" ? "¥" : currency + " "}${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
}

export function describeError(code: string): string {
  switch (code) {
    case "PAYMENT_PROVIDER_NOT_CONFIGURED":
      return "Usage is read-only here: no payment provider is configured, so no order can be paid.";
    case "TENANT_TOKEN_BUDGET_EXCEEDED":
      return "The token budget for this period is exhausted. Requests fail instead of overspending.";
    case "HTTP_401":
      return "Your session has expired. Sign in again.";
    case "HTTP_403":
      return "Your role does not allow billing changes.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export const pageLabels = [
  "Subscription & Usage Management",
  "Billing period",
  "Daily quota",
  "Monthly quota",
  "Usage history",
  "Orders",
  "No subscription is active",
  "Payment is unavailable",
  "Awaiting payment",
  "Usage values are authoritative",
];
