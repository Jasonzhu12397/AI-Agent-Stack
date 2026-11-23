import { request, tenantPath } from "../../services/http";
import type { BillingOverview, PaymentCapability, BillingPlan } from "./logic";

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
): Promise<{ id: string; status: string; error?: { code: string } }> {
  return request(`${tenantPath(tenantId, "/billing/orders/")}`, {
    method: "POST",
    body: JSON.stringify({ plan_code: planCode, channel, idempotency_key: idempotencyKey }),
  });
}
