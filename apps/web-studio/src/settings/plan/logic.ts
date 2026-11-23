export type BillingPlan = {
  code: string;
  name: string;
  description: string;
  interval: "monthly" | "yearly";
  price_cents: number;
  currency: string;
  token_quota_daily: number;
  token_quota_monthly: number;
  features: string[];
  region_scope: string;
  purchasable: boolean;
  reason_code: string | null;
};

export type PaymentCapability = {
  available: boolean;
  channels: string[];
  reason_code: string | null;
};

export type SubscriptionRecord = {
  plan_code: string;
  plan_name: string;
  status: string;
  current_period_start: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
};

export type BillingOverview = {
  capabilities: PaymentCapability;
  plans: BillingPlan[];
  subscription: SubscriptionRecord | null;
};

export const PAYMENT_UNAVAILABLE = "PAYMENT_PROVIDER_NOT_CONFIGURED";
export const REGION_UNSUPPORTED = "PURCHASE_REGION_NOT_SUPPORTED";

/** Channels the product supports; anything else does not exist server-side either. */
export const SUPPORTED_CHANNELS = ["wechat", "alipay"] as const;

export function paymentAvailability(capability?: PaymentCapability | null): {
  available: boolean;
  reasonCode: string;
} {
  if (!capability) return { available: false, reasonCode: PAYMENT_UNAVAILABLE };
  if (!capability.available) {
    return { available: false, reasonCode: capability.reason_code ?? PAYMENT_UNAVAILABLE };
  }
  const channels = (capability.channels ?? []).filter((channel) =>
    (SUPPORTED_CHANNELS as readonly string[]).includes(channel),
  );
  if (channels.length === 0) return { available: false, reasonCode: PAYMENT_UNAVAILABLE };
  return { available: true, reasonCode: "" };
}

/**
 * A plan is only purchasable when the server says so AND payment exists.
 * Region refusal must never be re-labelled as purchasable on the client.
 */
export function canPurchase(
  plan: Pick<BillingPlan, "purchasable" | "reason_code">,
  capability?: PaymentCapability | null,
): { ok: boolean; reasonCode: string | null } {
  const payment = paymentAvailability(capability);
  if (!payment.available) return { ok: false, reasonCode: payment.reasonCode };
  if (!plan.purchasable) {
    return { ok: false, reasonCode: plan.reason_code ?? REGION_UNSUPPORTED };
  }
  if (plan.reason_code) return { ok: false, reasonCode: plan.reason_code };
  return { ok: true, reasonCode: null };
}

/** Integer-only money formatting so 1299 never becomes 12.989999. */
export function formatPrice(cents: number, currency = "CNY"): string {
  if (!Number.isFinite(cents) || cents < 0) return `${currency} —`;
  const whole = Math.floor(cents / 100);
  const fraction = String(cents % 100).padStart(2, "0");
  const symbol = currency === "CNY" ? "¥" : `${currency} `;
  return `${symbol}${whole}.${fraction}`;
}

export function intervalLabel(interval: string): string {
  return interval === "yearly" ? "per year" : "per month";
}

export function quotaSummary(plan: BillingPlan): string {
  return `${plan.token_quota_daily.toLocaleString("en-US")} tokens / day · ${plan.token_quota_monthly.toLocaleString(
    "en-US",
  )} tokens / month`;
}

export function describeError(code: string): string {
  switch (code) {
    case PAYMENT_UNAVAILABLE:
      return "Payment is unavailable because no payment provider is configured.";
    case REGION_UNSUPPORTED:
      return "This plan includes models that cannot be purchased in this region.";
    case "PAYMENT_CHANNEL_NOT_SUPPORTED":
      return "Only WeChat Pay and Alipay are supported.";
    case "HTTP_401":
      return "Your session has expired. Sign in again.";
    case "HTTP_403":
      return "Your role does not allow plan changes.";
    default:
      return code ? `Request failed (${code}).` : "Request failed.";
  }
}

export const pageLabels = [
  "Standard Plan",
  "Current plan",
  "Included capabilities",
  "Plan limits",
  "Payment is unavailable",
  "No plans are available",
  "Purchase",
  "Payment channels",
  "Upgrade",
  "Plan management is not available in this deployment",
];
