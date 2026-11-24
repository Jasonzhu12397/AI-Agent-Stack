import { request, tenantPath } from "../../services/http";

export type NotificationEndpoint = {
  id: string;
  channel: "dingtalk" | "wecom" | "feishu";
  name: string;
  enabled: boolean;
  last_sent_at: string | null;
  last_error: string;
};

export const ROBOT_CHANNELS = [
  { value: "dingtalk", label: "钉钉群机器人", note: "加签密钥必填（安全设置选加签）" },
  { value: "wecom", label: "企业微信群机器人", note: "Webhook 里已带 key，不需要加签" },
  { value: "feishu", label: "飞书群机器人", note: "开启签名校验时填密钥" },
] as const;

export async function listEndpoints(tenantId: string): Promise<NotificationEndpoint[]> {
  const response = await request<{ results: NotificationEndpoint[] }>(
    tenantPath(tenantId, "/settings/notifications/endpoints/"),
  );
  return response.results;
}

export async function createEndpoint(
  tenantId: string,
  payload: { channel: string; name: string; webhook_url: string; secret: string },
): Promise<NotificationEndpoint> {
  return request<NotificationEndpoint>(tenantPath(tenantId, "/settings/notifications/endpoints/"), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function deleteEndpoint(tenantId: string, id: string): Promise<void> {
  await request<void>(tenantPath(tenantId, `/settings/notifications/endpoints/${id}/`), {
    method: "DELETE",
  });
}

export async function testEndpoint(
  tenantId: string,
  id: string,
): Promise<{ ok: boolean; message: string }> {
  return request<{ ok: boolean; message: string }>(
    tenantPath(tenantId, `/settings/notifications/endpoints/${id}/test/`),
    { method: "POST", body: JSON.stringify({}) },
  );
}
