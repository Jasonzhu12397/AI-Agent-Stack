import { request, tenantPath } from "../../services/http";
import type { OrderRecord, SubscriptionBundle, UsageSnapshot } from "./logic";

export async function fetchSubscriptionBundle(tenantId: string): Promise<SubscriptionBundle> {
  const [subscription, usage, orders] = await Promise.all([
    request<{ subscription: SubscriptionBundle["subscription"]; source: string }>(
      tenantPath(tenantId, "/billing/subscription/"),
    ),
    request<UsageSnapshot>(tenantPath(tenantId, "/billing/usage/")),
    request<{ results: OrderRecord[] }>(tenantPath(tenantId, "/billing/orders/")),
  ]);
  return { subscription: subscription.subscription, usage, orders: orders.results };
}

export async function closeOrder(tenantId: string, orderId: string): Promise<OrderRecord> {
  return request(tenantPath(tenantId, `/billing/orders/${orderId}/close/`), { method: "POST" });
}

/**
 * Refund a slice or the remainder of a paid order. The server generates the
 * provider refund reference per slice; the UI only states amount and reason.
 */
export async function refundOrder(
  tenantId: string,
  orderId: string,
  amountCents: number | null,
  reason: string,
): Promise<OrderRecord> {
  return request(tenantPath(tenantId, `/billing/orders/${orderId}/refund/`), {
    method: "POST",
    body: JSON.stringify({ amount_cents: amountCents, reason }),
  });
}

export type ReconciliationDiscrepancy = {
  order_id: string;
  channel: string;
  kind: "provider_not_found" | "status_mismatch" | "amount_mismatch";
  local_status?: string;
  provider_status: string;
  local_amount_cents?: number;
  provider_amount_cents?: number | null;
};

export type RefundDiscrepancy = {
  order_id: string;
  channel: string;
  kind: "refund_status_mismatch" | "refund_amount_mismatch";
  refund_reference: string;
  local_refund_cents: number;
  provider_refund_cents: number | null;
  provider_status: string;
};

export type ReconciliationReport = {
  start: string;
  end: string;
  order_count: number;
  status_counts: Record<string, number>;
  channel_amounts: Record<
    string,
    { order_count: number; amount_cents: number; refunded_cents: number }
  >;
  refund_slice_count: number;
  unavailable_channels: string[];
  discrepancies: ReconciliationDiscrepancy[];
  refund_discrepancies: RefundDiscrepancy[];
};

/**
 * Read-only reconciliation window. Dates are local ``YYYY-MM-DD`` boundaries;
 * the server never changes order state while building this report.
 */
export async function fetchReconciliation(
  tenantId: string,
  start: string,
  end: string,
): Promise<ReconciliationReport> {
  const query = new URLSearchParams({ start, end }).toString();
  return request(
    tenantPath(tenantId, `/billing/reconciliation/?${query}`),
  );
}

export const CANCEL_MODES = ["period_end", "immediate"] as const;
export type CancelMode = (typeof CANCEL_MODES)[number];

/** Schedule (``period_end``) or perform (``immediate``) a cancellation. */
export async function cancelSubscription(
  tenantId: string,
  mode: CancelMode,
): Promise<{ subscription: unknown }> {
  return request(tenantPath(tenantId, "/billing/subscription/cancel/"), {
    method: "POST",
    body: JSON.stringify({ mode }),
  });
}

/** Clear a scheduled cancellation; the subscription keeps renewing. */
export async function resumeSubscription(
  tenantId: string,
): Promise<{ subscription: unknown }> {
  return request(tenantPath(tenantId, "/billing/subscription/resume/"), {
    method: "POST",
  });
}
