import { request, tenantPath } from "../../services/http";
import type { BillingOverview, PaymentCapability, BillingPlan } from "./logic";

/** Checkout instructions returned by the provider when a trade is opened. */
export type PaymentInstruction = {
  provider?: string;
  trade_no?: string;
  prepay_id?: string;
  code_url?: string;
  qr_code?: string;
};

export type OrderResponse = {
  id: string;
  status: string;
  channel: string;
  amount_cents: number;
  currency: string;
  paid_at: string | null;
  payment?: PaymentInstruction;
  provider_status?: string;
  provider_error?: string;
  error?: { code: string };
};

export async function fetchBillingOverview(tenantId: string): Promise<BillingOverview> {
  const [capability, plans, subscription] = await Promise.all([
    request<PaymentCapability>(tenantPath(tenantId, "/billing/capabilities/")),
    request<{ results: BillingPlan[] }>(tenantPath(tenantId, "/billing/plans/")),
    request<{ subscription: BillingOverview["subscription"] }>(tenantPath(tenantId, "/billing/subscription/")),
  ]);
  return { capabilities: capability, plans: plans.results, subscription: subscription.subscription };
}

export async function createOrder(
  tenantId: string,
  planCode: string,
  channel: "wechat" | "alipay",
  idempotencyKey: string,
): Promise<OrderResponse> {
  return request(`${tenantPath(tenantId, "/billing/orders/")}`, {
    method: "POST",
    body: JSON.stringify({ plan_code: planCode, channel, idempotency_key: idempotencyKey }),
  });
}

/**
 * Settlement poll. Read-only on the server: it reports the local state plus
 * the provider's view and never moves the state machine, so polling can never
 * mark an order paid by itself.
 */
export async function fetchOrderStatus(tenantId: string, orderId: string): Promise<OrderResponse> {
  return request(tenantPath(tenantId, `/billing/orders/${orderId}/status/`));
}

export async function closeOrder(tenantId: string, orderId: string): Promise<OrderResponse> {
  return request(tenantPath(tenantId, `/billing/orders/${orderId}/close/`), { method: "POST" });
}
