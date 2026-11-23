import { request, tenantPath } from "../../services/http";
import type { NotificationChannel, NotificationMatrix } from "./logic";

type SectionEnvelope<T> = { values: T; defaults: T; effective: T; updated_at: string | null };

export async function fetchTenantNotifications(tenantId: string): Promise<SectionEnvelope<{ matrix: NotificationMatrix; digest_frequency: string }>> {
  return request(tenantPath(tenantId, "/settings/notifications/"));
}

export async function saveTenantNotifications(
  tenantId: string,
  values: { matrix: NotificationMatrix; digest_frequency: string },
): Promise<SectionEnvelope<{ matrix: NotificationMatrix; digest_frequency: string }>> {
  return request(tenantPath(tenantId, "/settings/notifications/"), {
    method: "PATCH",
    body: JSON.stringify({ values }),
  });
}

export async function fetchDeliveryPreferences(): Promise<SectionEnvelope<Partial<Record<NotificationChannel, boolean>>>> {
  return request("/api/v1/users/me/preferences/notification-delivery/");
}

export async function saveDeliveryPreferences(
  values: Partial<Record<NotificationChannel, boolean>>,
): Promise<SectionEnvelope<Partial<Record<NotificationChannel, boolean>>>> {
  return request("/api/v1/users/me/preferences/notification-delivery/", {
    method: "PUT",
    body: JSON.stringify({ values }),
  });
}
