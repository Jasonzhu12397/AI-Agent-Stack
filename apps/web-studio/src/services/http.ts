const apiBase = import.meta.env?.VITE_API_BASE_URL ?? "";

export type ApiErrorPayload = {
  code?: string;
  message?: string;
  details?: Record<string, string[] | string>;
};

/** Error raised for every non-2xx response. `code` is the server error code when present. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, string[] | string>;

  constructor(status: number, code: string, message: string, details: Record<string, string[] | string> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

let csrfCache: string | null = null;

async function csrfToken(): Promise<string> {
  if (csrfCache) return csrfCache;
  const response = await fetch(`${apiBase}/api/v1/auth/csrf/`, { credentials: "include" });
  if (!response.ok) throw new ApiError(response.status, "CSRF_UNAVAILABLE", "Unable to obtain a CSRF token.");
  const payload = (await response.json()) as { csrf_token?: string; csrfToken?: string };
  // The API answers snake_case; the camelCase fallback keeps older payloads working.
  csrfCache = payload.csrf_token ?? payload.csrfToken ?? "";
  if (!csrfCache) throw new ApiError(response.status, "CSRF_UNAVAILABLE", "The server returned an empty CSRF token.");
  return csrfCache;
}

function readError(status: number, payload: Record<string, unknown>): ApiError {
  const envelope = payload.error as ApiErrorPayload | undefined;
  if (envelope && typeof envelope === "object") {
    return new ApiError(
      status,
      envelope.code ?? `HTTP_${status}`,
      envelope.message ?? `Request failed with HTTP ${status}`,
      envelope.details ?? {},
    );
  }
  if (typeof payload.detail === "string") {
    return new ApiError(status, `HTTP_${status}`, payload.detail);
  }
  return new ApiError(status, `HTTP_${status}`, `Request failed with HTTP ${status}`);
}

/** Same transport conventions as services/api.ts, but exposes the server error code. */
export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  // Proxies in front of this origin may reject DELETE/PATCH/PUT outright, so
  // those travel as POST with an override header the API turns back.
  const tunnelled = ["DELETE", "PATCH", "PUT"].includes(method);
  const wireMethod = tunnelled ? "POST" : method;
  if (tunnelled) headers.set("X-HTTP-Method-Override", method);
  if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
    headers.set("X-CSRFToken", await csrfToken());
  }
  const response = await fetch(`${apiBase}${path}`, { ...init, method: wireMethod, headers, credentials: "include" });
  if (response.status === 204) return undefined as T;
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) throw readError(response.status, payload);
  return payload as T;
}

export function tenantPath(tenantId: string, suffix: string): string {
  return `/api/v1/tenants/${tenantId}${suffix}`;
}

export function projectPath(tenantId: string, projectId: string, suffix: string): string {
  return `/api/v1/tenants/${tenantId}/projects/${projectId}${suffix}`;
}
