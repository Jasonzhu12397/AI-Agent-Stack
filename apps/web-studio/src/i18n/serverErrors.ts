import { locale } from "../i18n";

// Backend runtime failures travel in English because they are built in Python.
// The API already tags them with a stable code, so the UI translates through
// that code instead of matching prose. Values pulled out of the English
// message (provider name, HTTP status) are reused inside the Chinese text.
const SERVER_ERROR_ZH: Record<string, string> = {
  MODEL_CREDENTIAL_REJECTED: "{provider} 拒绝了该账号凭据（HTTP {status}）。请在平台的 API 页面粘贴新的密钥。",
  MODEL_CREDENTIAL_MISSING: "该模型缺少凭据。请先授权一个平台账号，或粘贴自己的密钥。",
  MODEL_PROVIDER_RATE_LIMITED: "模型服务触发速率限制或账户额度限制（HTTP 429）。请检查服务商的调用频率、账户额度和付款状态，稍后再试。",
  MODEL_PROVIDER_HTTP_ERROR: "模型服务返回 HTTP {status}。",
  MODEL_PROVIDER_UNAVAILABLE: "无法连接到模型服务，请稍后重试。",
  MODEL_PROVIDER_INVALID_RESPONSE: "模型服务返回了无法解析的内容。",
  MODEL_PROVIDER_RESPONSE_TOO_LARGE: "模型服务的响应超过 16 MiB 上限。",
  MODEL_PROVIDER_INVALID_EMBEDDING: "模型服务返回了无效的向量结果。",
  MODEL_DISABLED: "该模型已被停用，请重新选择一个模型。",
  MODEL_CAPABILITY_MISSING: "该模型不支持所选的能力。",
  MODEL_CONTEXT_WINDOW_EXCEEDED: "输入超过了该模型的上下文窗口。",
  MODEL_REQUEST_CONTEXT_TOO_LARGE: "请求上下文过大，请缩短输入后重试。",
  MODEL_ENDPOINT_SCHEME_INVALID: "模型地址的协议无效，只支持 https（本机可回环到 127.0.0.1）。",
  LOCAL_MODEL_HOST_NOT_ALLOWED: "该模型地址不在允许的本机白名单内。",
  CREDENTIAL_REFERENCE_UNSUPPORTED: "该凭据引用格式不被支持，请使用 env:NAME。",
  PROVIDER_ACCOUNT_NOT_LINKED: "尚未关联该平台账号，请先在模型管理页授权。",
  TOKEN_GUARD_REQUIRED: "该请求需要令牌守卫，请先在治理页启用。",
  INVALID_MESSAGES: "消息格式无效。",
  INVALID_MAX_TOKENS: "max_tokens 取值无效。",
  INVALID_TIMEOUT: "超时设置取值无效。",
  INVALID_TOOLS: "工具定义无效。",
  INVALID_TOOL_CHOICE: "tool_choice 取值无效。",
  EMBEDDING_INPUT_LIMIT: "向量输入条数超过上限。",
  INVALID_EMBEDDING_INPUT: "向量输入无效。",
  PROVIDER_UNSUPPORTED: "暂不支持该模型提供方。",
  INVALID_FAKE_RESPONSE: "预设回复内容无效。",
};

function pick(pattern: RegExp, value: string): string {
  const match = value.match(pattern);
  return match ? match[1] : "";
}

/**
 * Translate a backend error into the active locale. Unknown codes fall back to
 * the original English text so nothing is silently swallowed.
 */
export function tServerError(code: string | null | undefined, message: string): string {
  if (locale.value === "en" || !code) return message;
  const template = SERVER_ERROR_ZH[code];
  if (!template) return message;
  return template
    .replace("{provider}", pick(/^(.+?) rejected the account credential/, message))
    .replace("{status}", pick(/HTTP (\d{3})/, message) || "错误");
}
