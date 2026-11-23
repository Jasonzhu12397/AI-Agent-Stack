export type UsageSnapshot = {
  daily_limit: number;
  monthly_limit: number;
  daily_used: number;
  monthly_used: number;
  remaining_daily?: number | null;
  remaining_monthly?: number | null;
  reset_at?: string | null;
  monthly_reset_at?: string | null;
  limits_source?: string | null;
  period_start: string | null;
  period_end: string | null;
  history: { period: string; consumed_tokens: number; source: string }[];
};

export type OrderRecord = {
  id: string;
  plan_code: string;
  status:
    | "created"
    | "pending"
    | "paid"
    | "failed"
    | "closed"
    | "partial_refunded"
    | "refunded";
  amount_cents: number;
  refunded_cents: number;
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
    case "partial_refunded":
      return "Partially refunded";
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

export type QuotaPeriod = "daily" | "monthly";
export type QuotaState = "ok" | "warning" | "exhausted";

const QUOTA_WARNING_RATIO = 0.8;

function normalizePeriod(period: string | null | undefined): QuotaPeriod {
  return period === "monthly" ? "monthly" : "daily";
}

function safeUsed(used: number): number {
  return Number.isFinite(used) && used > 0 ? used : 0;
}

function safeLimit(limit: number): number {
  return Number.isFinite(limit) && limit > 0 ? limit : 0;
}

/**
 * Thresholds: >= 100% exhausted, >= 80% warning, otherwise ok. Boundaries are
 * exact so 79.9% stays ok while 80.0% raises the warning state.
 */
export function quotaState(used: number, limit: number): QuotaState {
  const ceiling = safeLimit(limit);
  if (ceiling <= 0) return "ok";
  const ratio = safeUsed(used) / ceiling;
  if (ratio >= 1) return "exhausted";
  if (ratio >= QUOTA_WARNING_RATIO) return "warning";
  return "ok";
}

/**
 * Remaining quota never renders negative: prefer the server-supplied remaining
 * value when it is a non-negative number, otherwise derive `limit - used`.
 */
export function remainingQuota(
  used: number,
  limit: number,
  remaining?: number | null,
): number {
  if (typeof remaining === "number" && Number.isFinite(remaining) && remaining >= 0) {
    return Math.max(0, remaining);
  }
  return Math.max(0, safeLimit(limit) - safeUsed(used));
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** Render an ISO timestamp as local `YYYY-MM-DD HH:mm`; invalid input shows `—`. */
export function formatResetAt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const PERIOD_COPY: Record<QuotaPeriod, string> = {
  daily: "每日",
  monthly: "每月",
};

/**
 * User-facing quota copy. The exhausted state carries the reset time when the
 * server provides one; missing reset time is never fabricated.
 */
export function quotaMessage(
  state: QuotaState,
  period: string | null | undefined,
  options: { percent?: number; resetAt?: string | null } = {},
): string {
  const label = PERIOD_COPY[normalizePeriod(period)];
  if (state === "exhausted") {
    const reset = options.resetAt
      ? `将于 ${formatResetAt(options.resetAt)} 重置，`
      : "";
    return `本期${label}额度已用尽，${reset}或升级套餐后继续使用。`;
  }
  if (state === "warning") {
    const percent = Number.isFinite(options.percent ?? NaN) ? Math.round(options.percent ?? 0) : 0;
    return `已使用 ${percent}%，接近${label}额度上限。`;
  }
  return `${label}额度使用正常。`;
}

/**
 * Map budget-guard failures to a blocking copy that names the day/month
 * period. Any other code falls back to the generic mapping; a 429 is never
 * rendered as "no data".
 */
export function mapQuotaError(
  code: string,
  period?: string | null,
  resetAt?: string | null,
): string {
  if (code === "TENANT_TOKEN_BUDGET_EXCEEDED") {
    const label = PERIOD_COPY[normalizePeriod(period)];
    const reset = resetAt ? `，将于 ${formatResetAt(resetAt)} 重置` : "";
    return `${label}额度已用尽，本次请求已被阻断${reset}，升级套餐后可继续使用。`;
  }
  return describeError(code);
}

export function describeError(code: string): string {
  switch (code) {
    case "PAYMENT_PROVIDER_NOT_CONFIGURED":
      return "Usage is read-only here: no payment provider is configured, so no order can be paid.";
    case "TENANT_TOKEN_BUDGET_EXCEEDED":
      return "The token budget for this period is exhausted. Requests fail instead of overspending.";
    case "ORDER_ALREADY_PAID":
      return "The provider reports this order as paid. Refund it instead of closing it.";
    case "PAYMENT_QUERY_FAILED":
      return "The payment provider could not be queried. Try again; the order stays open.";
    case "PAYMENT_PROVIDER_RESPONSE_UNTRUSTED":
      return "The payment provider response failed signature verification. Nothing was changed.";
    case "PAYMENT_PROVIDER_UPSTREAM_ERROR":
      return "The payment provider rejected the request. Nothing was changed; try again later.";
    case "REFUND_AMOUNT_EXCEEDED":
      return "The refund exceeds the remaining refundable amount.";
    case "REFUND_AMOUNT_INVALID":
      return "The refund amount must be a positive amount within the refundable remainder.";
    case "ORDER_STATE_CONFLICT":
      return "This order can no longer change state.";
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
  "Remaining",
  "Resets at",
  "Quota exhausted: requests that consume quota are blocked.",
  "Usage history",
  "Orders",
  "No subscription is active",
  "Payment is unavailable",
  "Awaiting payment",
  "Usage values are authoritative",
];
