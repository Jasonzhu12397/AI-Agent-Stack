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
