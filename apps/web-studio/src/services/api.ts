const apiBase = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export type CurrentUser = {
  id: number;
  username: string;
  email: string;
  is_staff: boolean;
};

export type Tenant = { id: string; name: string; slug: string };
export type Project = { id: string; tenant_id: string; name: string; slug: string };
export type Task = {
  id: string;
  task_type: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  status: string;
  attempts: number;
  created_at: string;
  finished_at: string | null;
};

type Page<T> = { count: number; results: T[] };
type CsrfResponse = { csrf_token: string };

async function csrfToken(): Promise<string> {
  const response = await fetch(`${apiBase}/api/v1/auth/csrf/`, {
    credentials: "include",
  });
  if (!response.ok) throw new Error("Unable to initialize secure session.");
  const result = (await response.json()) as CsrfResponse;
  return result.csrf_token;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
    headers.set("X-CSRFToken", await csrfToken());
  }
  const response = await fetch(`${apiBase}${path}`, {
    ...init,
    method,
    headers,
    credentials: "include",
  });
  if (response.status === 204) return undefined as T;
  const payload = (await response.json()) as T & { detail?: string };
  if (!response.ok) {
    throw new Error(payload.detail ?? `Request failed with HTTP ${response.status}`);
  }
  return payload;
}

export function getCurrentUser(): Promise<CurrentUser> {
  return request<CurrentUser>("/api/v1/auth/me/");
}

export function login(username: string, password: string): Promise<CurrentUser> {
  return request<CurrentUser>("/api/v1/auth/login/", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function logout(): Promise<void> {
  return request<void>("/api/v1/auth/logout/", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function getTenants(): Promise<Page<Tenant>> {
  return request<Page<Tenant>>("/api/v1/tenants/");
}

export function createTenant(name: string, slug: string): Promise<Tenant> {
  return request<Tenant>("/api/v1/tenants/", {
    method: "POST",
    body: JSON.stringify({ name, slug }),
  });
}

export function getProjects(tenantId: string): Promise<Page<Project>> {
  return request<Page<Project>>(`/api/v1/tenants/${tenantId}/projects/`);
}

export function createProject(
  tenantId: string,
  name: string,
  slug: string,
): Promise<Project> {
  return request<Project>(`/api/v1/tenants/${tenantId}/projects/`, {
    method: "POST",
    body: JSON.stringify({ name, slug }),
  });
}

export function getTasks(tenantId: string, projectId: string): Promise<Page<Task>> {
  return request<Page<Task>>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/tasks/?limit=50&offset=0`,
  );
}

export function createEchoTask(
  tenantId: string,
  projectId: string,
  message: string,
): Promise<Task> {
  return request<Task>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/tasks/`,
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ task_type: "demo.echo", payload: { message } }),
    },
  );
}

export function cancelTask(taskId: string): Promise<Task> {
  return request<Task>(`/api/v1/tasks/${taskId}/cancel/`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export { apiBase };
