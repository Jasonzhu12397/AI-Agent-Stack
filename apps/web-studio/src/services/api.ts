// Empty base means same-origin: the API serves the console bundle, and the
// dev server proxies /api, so both modes need no cross-origin configuration.
const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";

export type CurrentUser = {
  id: number;
  username: string;
  email: string;
  is_staff: boolean;
  session_token?: string;
};

// Access proxies and in-app webviews can partition or drop the session cookie,
// which turned every call after sign-in into a 401. The login response also
// carries the server-side session key, and it is sent as a header from then on.
const SESSION_TOKEN_KEY = "agent-stack-session-token";

export function storedSessionToken(): string {
  try {
    return window.localStorage.getItem(SESSION_TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setSessionToken(token: string): void {
  try {
    if (token) window.localStorage.setItem(SESSION_TOKEN_KEY, token);
    else window.localStorage.removeItem(SESSION_TOKEN_KEY);
  } catch {
    // Storage can be unavailable in a private webview; cookies still work.
  }
}

export type Tenant = { id: string; name: string; slug: string };
export type Project = { id: string; tenant_id: string; name: string; slug: string };
export type ProviderAccount = {
  linked: boolean;
  account_label?: string;
  linked_at?: string | null;
  credential_configured?: boolean;
  verification?: { verified: boolean | null; reason: string };
  free_daily_tokens: number;
  used_today: number;
  remaining_today: number;
  exhausted: boolean;
};
export type ModelProvider = {
  id: string;
  name: string;
  provider_type: string;
  auth_mode: "tenant_secret" | "user_account" | "platform_pool";
  preset_key: string;
  base_url: string;
  credential_configured: boolean;
  account: ProviderAccount | null;
  enabled: boolean;
};
export type ProviderPreset = {
  platform_env?: string;
  platform_configured?: boolean;
  key: string;
  label: string;
  base_url: string;
  models: { model_name: string; display_name: string }[];
  //: Platforms that can grant access themselves offer a one-click flow.
  oauth?: boolean;
  //: Vendor page that issues an API key, opened from the authorize dialog.
  console_url?: string;
};
export type ProviderCatalog = {
  presets: ProviderPreset[];
  free_daily_tokens: number;
};
export type ModelConfig = {
  id: string;
  provider_id: string;
  model_name: string;
  display_name: string;
  capabilities: string[];
  context_window: number;
  max_output_tokens: number;
  version: number;
};
export type AgentDraft = {
  id: string;
  name: string;
  slug: string;
  description: string;
  draft_config: Record<string, unknown>;
  current_version: number;
  is_archived: boolean;
};
export type AgentVersion = {
  id: string;
  agent_id: string;
  version: number;
  config_snapshot: Record<string, unknown>;
  published_at: string;
};
export type AgentRun = {
  id: string;
  task_id: string;
  agent_version_id: string;
  task_status: string;
  input_text: string;
  output_text: string;
  input_tokens: number;
  output_tokens: number;
  provider_name: string;
  model_name: string;
  latency_ms: number;
  created_at: string;
  finished_at: string | null;
};
export type AgentTeamDraft = {
  id: string;
  name: string;
  slug: string;
  description: string;
  draft_config: Record<string, unknown>;
  current_version: number;
  is_archived: boolean;
};
export type AgentTeamVersion = {
  id: string;
  team_id: string;
  version: number;
  definition_snapshot: Record<string, unknown>;
  workflow_version_id: string;
  published_at: string;
};
export type AgentTeamRun = {
  id: string;
  team_version_id: string;
  task_id: string;
  task_status: string;
  input_text: string;
  output_text: string;
  created_at: string;
  finished_at: string | null;
};
export type ModelTestResult = {
  content: string;
  input_tokens: number;
  output_tokens: number;
  provider: string;
  model: string;
  latency_ms: number;
};
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
    // A cached token no longer matches the cookie the browser holds.
    cache: "no-store",
  });
  if (!response.ok) {
    // A 404/403 here means the workspace or its forwarded port is not answering,
    // not that the credentials are wrong; say so instead of blaming the session.
    throw new ApiError(
      `Unable to initialize secure session (HTTP ${response.status}). ` +
        "If this repeats, the workspace may be starting or its public port is offline.",
      {},
      response.status,
    );
  }
  const result = (await response.json()) as CsrfResponse;
  return result.csrf_token;
}

export class ApiError extends Error {
  data: Record<string, unknown>;
  readonly status: number;

  constructor(message: string, data: Record<string, unknown>, status = 0) {
    super(message);
    this.name = "ApiError";
    this.data = data;
    this.status = status;
  }
}

// Read the programmable error code carried by a backend error body. Returns
// null when the error is not an ApiError or the body has no code field.
export function apiErrorCode(error: unknown): string | null {
  if (error instanceof ApiError && error.data && typeof error.data.code === "string") {
    return error.data.code;
  }
  return null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const requested = (init.method ?? "GET").toUpperCase();
  // Hosting proxies can reject DELETE/PATCH/PUT outright; send them as POST
  // with an override header that the API turns back into the real verb.
  const tunnelled = ["DELETE", "PATCH", "PUT"].includes(requested);
  const method = tunnelled ? "POST" : requested;
  const headers = new Headers(init.headers);
  if (tunnelled) headers.set("X-HTTP-Method-Override", requested);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const sessionToken = storedSessionToken();
  if (sessionToken) headers.set("X-Session-Token", sessionToken);
  if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(method)) {
    headers.set("X-CSRFToken", await csrfToken());
  }
  let response = await fetch(`${apiBase}${path}`, {
    ...init,
    method,
    headers,
    credentials: "include",
  });
  if (
    response.status === 403 &&
    !["GET", "HEAD", "OPTIONS", "TRACE"].includes(method) &&
    headers.has("X-CSRFToken")
  ) {
    // The token can go stale when a proxy serves a cached token endpoint (or
    // the cookie rotated); re-issue it once and retry the same write.
    headers.set("X-CSRFToken", await csrfToken());
    response = await fetch(`${apiBase}${path}`, {
      ...init,
      method,
      headers,
      credentials: "include",
    });
  }
  if (response.status === 401) {
    // Credentials are gone (expired cookie, restarted database, proxy gate).
    // Every view would otherwise show one unrelated error each, so the shell
    // listens for one event and sends the user back to the sign-in page.
    // A console that boots on the sign-in route probes the API before anyone
    // has signed in; those probes are not an expired session, and treating
    // them as one left a red "session expired" banner on a fresh sign-in.
    if (storedSessionToken()) {
      window.dispatchEvent(new Event("agent-stack:session-expired"));
    }
  }
  if (response.status === 204) return undefined as T;
  // An access proxy in front of this origin answers with HTML, not the API
  // envelope. Parsing it as JSON would bury the real reason in a syntax error,
  // so read the body once as text and only then try to decode it.
  const raw = await response.text();
  let payload: (T & { detail?: string }) | null = null;
  try {
    payload = raw ? (JSON.parse(raw) as T & { detail?: string }) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const body = (payload ?? {}) as Record<string, unknown>;
    const detail =
      (payload && typeof payload.detail === "string" && payload.detail) ||
      (typeof body.error === "object" &&
        body.error !== null &&
        typeof (body.error as { message?: unknown }).message === "string" &&
        (body.error as { message: string }).message) ||
      null;
    if (detail) throw new ApiError(detail, body, response.status);
    if (response.status === 403 && payload === null) {
      throw new ApiError(
        "This origin is behind an access proxy and rejected the request. Reload the page and sign in to the proxy, then try again.",
        { status: response.status },
        response.status,
      );
    }
    if ([502, 503, 504].includes(response.status) && payload === null) {
      throw new ApiError(
        `The platform gateway returned HTTP ${response.status} before the API answered. ` +
          "The upstream model service may be unavailable; retry in a moment or check the provider status.",
        { status: response.status },
        response.status,
      );
    }
    throw new ApiError(`Request failed with HTTP ${response.status}`, body, response.status);
  }
  return (payload ?? ({} as T)) as T;
}

export function getCurrentUser(): Promise<CurrentUser> {
  return request<CurrentUser>("/api/v1/auth/me/");
}

export async function login(username: string, password: string): Promise<CurrentUser> {
  const user = await request<CurrentUser>("/api/v1/auth/login/", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  if (user.session_token) setSessionToken(user.session_token);
  return user;
}

export async function register(
  identifier: string,
  password: string,
  displayName: string,
): Promise<CurrentUser> {
  const user = await request<CurrentUser>("/api/v1/auth/register/", {
    method: "POST",
    body: JSON.stringify({
      identifier,
      password,
      display_name: displayName,
    }),
  });
  if (user.session_token) setSessionToken(user.session_token);
  return user;
}

export async function logout(): Promise<void> {
  await request<void>("/api/v1/auth/logout/", {
    method: "POST",
    body: JSON.stringify({}),
  });
  setSessionToken("");
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

export function getModelProviders(tenantId: string): Promise<ModelProvider[]> {
  return request<ModelProvider[]>(`/api/v1/tenants/${tenantId}/model-providers/`);
}

export function createFakeProvider(
  tenantId: string,
  name: string,
  response: string,
): Promise<ModelProvider> {
  return request<ModelProvider>(`/api/v1/tenants/${tenantId}/model-providers/`, {
    method: "POST",
    body: JSON.stringify({
      name,
      provider_type: "fake",
      base_url: "",
      credential_ref: "",
      config: { response },
    }),
  });
}

export function getModelConfigs(
  tenantId: string,
  providerId: string,
): Promise<ModelConfig[]> {
  return request<ModelConfig[]>(
    `/api/v1/tenants/${tenantId}/model-providers/${providerId}/models/`,
  );
}

export function createModelConfig(
  tenantId: string,
  providerId: string,
  modelName: string,
  displayName: string,
  capabilities: string[] = ["chat", "tools"],
): Promise<ModelConfig> {
  return request<ModelConfig>(
    `/api/v1/tenants/${tenantId}/model-providers/${providerId}/models/`,
    {
      method: "POST",
      body: JSON.stringify({
        model_name: modelName,
        display_name: displayName,
        capabilities,
        context_window: 32768,
        max_output_tokens: 4096,
      }),
    },
  );
}

export function getAgents(tenantId: string, projectId: string): Promise<AgentDraft[]> {
  return request<AgentDraft[]>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/agents/`,
  );
}

export function createAgent(
  tenantId: string,
  projectId: string,
  payload: {
    name: string;
    slug: string;
    description: string;
    draft_config: Record<string, unknown>;
  },
): Promise<AgentDraft> {
  return request<AgentDraft>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/agents/`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function publishAgent(agentId: string): Promise<AgentVersion> {
  return request<AgentVersion>(`/api/v1/agents/${agentId}/publish/`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function getAgentRuns(agentId: string): Promise<AgentRun[]> {
  return request<AgentRun[]>(`/api/v1/agents/${agentId}/runs/`);
}

export function createAgentRun(agentId: string, input: string): Promise<AgentRun> {
  return request<AgentRun>(`/api/v1/agents/${agentId}/runs/`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ input }),
  });
}

export function getAgentTeams(
  tenantId: string,
  projectId: string,
): Promise<AgentTeamDraft[]> {
  return request<AgentTeamDraft[]>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/agent-teams/`,
  );
}

export function createAgentTeam(
  tenantId: string,
  projectId: string,
  payload: {
    name: string;
    slug: string;
    description: string;
    draft_config: Record<string, unknown>;
  },
): Promise<AgentTeamDraft> {
  return request<AgentTeamDraft>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/agent-teams/`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function updateAgentTeam(
  teamId: string,
  payload: Partial<Pick<AgentTeamDraft, "name" | "slug" | "description" | "draft_config">>,
): Promise<AgentTeamDraft> {
  return request<AgentTeamDraft>(`/api/v1/agent-teams/${teamId}/`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function publishAgentTeam(teamId: string): Promise<AgentTeamVersion> {
  return request<AgentTeamVersion>(`/api/v1/agent-teams/${teamId}/publish/`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function getAgentTeamRuns(teamId: string): Promise<AgentTeamRun[]> {
  return request<AgentTeamRun[]>(`/api/v1/agent-teams/${teamId}/runs/`);
}

export function createAgentTeamRun(teamId: string, input: string): Promise<AgentTeamRun> {
  return request<AgentTeamRun>(`/api/v1/agent-teams/${teamId}/runs/`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ input }),
  });
}

export function testModel(
  tenantId: string,
  modelConfigId: string,
  message: string,
): Promise<ModelTestResult> {
  return request<ModelTestResult>(
    `/api/v1/tenants/${tenantId}/model-configs/${modelConfigId}/test/`,
    {
      method: "POST",
      body: JSON.stringify({
        messages: [{ role: "user", content: message }],
      }),
    },
  );
}

export { apiBase };


export type WorkflowCatalogEntry = { id: string; label?: string; name?: string };
export type WorkflowCatalog = {
  models: { id: string; label: string; provider: string; model_name: string }[];
  knowledge_bases: { id: string; name: string }[];
  tools: { id: string; name: string; risk_level: string; plugin: string }[];
  agents: { id: string; name: string; version: number }[];
};

export type WorkflowDraft = {
  id: string;
  name: string;
  slug: string;
  description: string;
  draft_definition: Record<string, unknown>;
  current_version: number;
  is_archived: boolean;
};

export type WorkflowStepRun = {
  id: string;
  node_id: string;
  node_type: string;
  status: string;
  input_data: Record<string, unknown>;
  output_data: unknown;
  error: Record<string, unknown> | null;
  attempt: number;
  started_at: string | null;
  finished_at: string | null;
};

export type WorkflowRun = {
  id: string;
  task_id: string;
  workflow_version_id: string;
  task_status: string;
  task_error?: { code?: string; message?: string } | null;
  input_data: Record<string, unknown>;
  state: Record<string, unknown>;
  output_data: unknown;
  steps: WorkflowStepRun[];
  created_at: string;
  finished_at: string | null;
};

export type WorkflowValidationResult = {
  is_valid: boolean;
  errors: string[];
  execution_layers: string[][];
  node_count: number;
  edge_count: number;
};

export type WorkflowVersion = {
  id: string;
  workflow_id: string;
  version: number;
  definition_snapshot: Record<string, unknown>;
  published_at: string;
};

export function getWorkflowCatalog(
  tenantId: string,
  projectId: string,
): Promise<WorkflowCatalog> {
  return request<WorkflowCatalog>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/workflow-catalog/`,
  );
}

export function getWorkflows(tenantId: string, projectId: string): Promise<WorkflowDraft[]> {
  return request<WorkflowDraft[]>(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/workflows/",
  );
}

export function createWorkflow(
  tenantId: string,
  projectId: string,
  payload: {
    name: string;
    slug: string;
    description: string;
    draft_definition: Record<string, unknown>;
  },
): Promise<WorkflowDraft> {
  return request<WorkflowDraft>(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/workflows/",
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function updateWorkflow(
  workflowId: string,
  payload: {
    name?: string;
    description?: string;
    draft_definition?: Record<string, unknown>;
  },
): Promise<WorkflowDraft> {
  return request<WorkflowDraft>("/api/v1/workflows/" + workflowId + "/", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function validateWorkflow(workflowId: string): Promise<WorkflowValidationResult> {
  return request<WorkflowValidationResult>("/api/v1/workflows/" + workflowId + "/validate/", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function publishWorkflow(workflowId: string): Promise<WorkflowVersion> {
  return request<WorkflowVersion>("/api/v1/workflows/" + workflowId + "/publish/", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function getWorkflowRuns(workflowId: string): Promise<WorkflowRun[]> {
  return request<WorkflowRun[]>("/api/v1/workflows/" + workflowId + "/runs/");
}

export function decideWorkflowStep(
  runId: string,
  nodeId: string,
  decision: "approve" | "reject",
  reason = "",
): Promise<{ run_id: string; node_id: string; decision: string; task_status: string }> {
  return request(
    "/api/v1/workflow-runs/" + runId + "/steps/" + encodeURIComponent(nodeId) + "/" + decision + "/",
    { method: "POST", body: JSON.stringify({ reason }) },
  );
}


export function createWorkflowRun(
  workflowId: string,
  input: Record<string, unknown>,
): Promise<WorkflowRun> {
  return request<WorkflowRun>("/api/v1/workflows/" + workflowId + "/runs/", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ input }),
  });
}


export type AgentNode = {
  id: string;
  tenant_id: string;
  project_id: string;
  name: string;
  node_type: "eph_node" | "target_cluster";
  cluster_name: string;
  agent_version: string;
  capabilities: string[];
  labels: Record<string, string>;
  status: "offline" | "online" | "revoked";
  last_seen_at: string | null;
  last_seen_ip: string | null;
  created_at: string;
};

export type FleetMachine = AgentNode & {
  observed_status: "offline" | "online" | "revoked" | "stale";
  machine_id: string | null;
  lifecycle_state: string | null;
  lifecycle_targets: string[];
  bmc_configured: boolean;
  bmc_host: string | null;
  bmh_name: string;
  inventory_revision: number;
  identity: {
    agent_node_id: string;
    name: string;
    cluster_name: string;
    system_uuid: string;
    machine_id: string | null;
    identity_status: "unverified" | "candidate" | "verified" | "conflict";
    identity_fingerprint: string | null;
    identity_conflicts: Array<{
      machine_id: string;
      agent_node_id: string;
      matching_fields: string[];
    }>;
  };
  hardware_inventory: Record<string, unknown>;
  inventory_source: string;
  observed_at: string | null;
};

export function configureFleetMachineBmc(
  tenantId: string,
  projectId: string,
  machineId: string,
  body: { endpoint: string; credential_ref: string; trust_bundle_ref?: string },
): Promise<{ machine_id: string; bmc_configured: boolean; endpoint: string }> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId +
      "/fleet/machines/" + machineId + "/bmc-config/",
    { method: "PUT", body: JSON.stringify(body) },
  );
}

export function reviewFleetMachineLifecycle(
  tenantId: string,
  projectId: string,
  machineId: string,
  body: { expected_state: string; target_state: string; reason: string },
): Promise<{ machine_id: string; lifecycle_state: string }> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId +
      "/fleet/machines/" + machineId + "/lifecycle-review/",
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function getFleetMachines(
  tenantId: string,
  projectId: string,
  options: { limit?: number; offset?: number; search?: string; status?: string; nodeType?: string } = {},
): Promise<{
  count: number;
  limit: number;
  offset: number;
  results: FleetMachine[];
  capabilities: { hardware_inventory: string; stable_hardware_identity: string };
}> {
  const query = new URLSearchParams({
    limit: String(options.limit ?? 100),
    offset: String(options.offset ?? 0),
  });
  if (options.search) query.set("search", options.search);
  if (options.status) query.set("status", options.status);
  if (options.nodeType) query.set("node_type", options.nodeType);
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/fleet/machines/?" + query.toString(),
  );
}

export type HostNetworkFinding = {
  code: string;
  status: "pass" | "fail" | "unknown";
  network: string | null;
  interface: string | null;
  expected: unknown;
  observed: unknown;
};

export type HostNetworkVerification = {
  machine_id: string;
  machine_name: string;
  inventory_source: string;
  inventory_revision: number;
  observed_at: string | null;
  host_role: "control_plane" | "worker" | "storage";
  status: "pass" | "fail" | "unknown";
  ok: boolean;
  checked_networks: string[];
  findings: HostNetworkFinding[];
  observed_interfaces: string[];
  observed_nics: Array<Record<string, unknown> & {
    name: string;
    pci_address?: string;
    sriov_numvfs?: number;
    sriov_totalvfs?: number;
  }>;
};

export type PxeBootResult = {
  execution: boolean;
  machine_count: number;
  executed: number;
  results: Array<{
    machine_id: string;
    name: string;
    status: "planned" | "executed" | "skipped" | "failed";
    error_code?: string;
    evidence?: Record<string, unknown>;
  }>;
};

export type BmhInventoryIngest = {
  host_count: number;
  matched: number;
  unmatched: string[];
  warnings: string[];
  updated: Array<{
    machine_id: string;
    name: string;
    pci_addresses: string[];
    pci_inventory_adopted: boolean;
  }>;
};

export function ingestFleetBmhInventory(
  tenantId: string,
  projectId: string,
  document: unknown,
): Promise<BmhInventoryIngest> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/fleet/machines/bmh-inventory/",
    {
      method: "POST",
      body: JSON.stringify({ document }),
    },
  );
}

export function pxeBootFleetMachines(
  tenantId: string,
  projectId: string,
  execution: boolean,
  machineIds?: string[],
): Promise<PxeBootResult> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/fleet/machines/pxe-boot/",
    {
      method: "POST",
      body: JSON.stringify({
        execution,
        ...(execution ? { confirm: "PXE_ONCE" } : {}),
        ...(machineIds ? { machine_ids: machineIds } : {}),
      }),
    },
  );
}

export type FleetDiscoveryResult = {
  execution: boolean;
  machine_count: number;
  executed: number;
  results: PxeBootResult["results"];
  inspect: {
    queued: Array<{ node_id: string; command_id: string }>;
    skipped: Array<{ node_id: string; reason: string }>;
    reason: string | null;
  };
};

export function runFleetDiscovery(
  tenantId: string,
  projectId: string,
  machineIds?: string[],
): Promise<FleetDiscoveryResult> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/fleet/machines/discovery/",
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        execution: true,
        confirm: "PXE_ONCE",
        ...(machineIds ? { machine_ids: machineIds } : {}),
      }),
    },
  );
}

export function collectFleetNetwork(
  tenantId: string,
  projectId: string,
): Promise<{
  queued_count: number;
  skipped_count: number;
  queued: Array<{ node_id: string; command_id: string }>;
  skipped: Array<{ node_id: string; reason: string }>;
}> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/fleet/network/collect/",
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({}),
    },
  );
}

export function verifyFleetMachineNetwork(
  tenantId: string,
  projectId: string,
  machineId: string,
  hostRole: "control_plane" | "worker" | "storage",
): Promise<HostNetworkVerification> {
  const query = new URLSearchParams({ host_role: hostRole });
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId +
    "/fleet/machines/" + machineId + "/network/verify/?" + query.toString(),
  );
}

export type FirmwareInventoryItem = {
  id: string;
  name: string;
  version: string;
  updateable: boolean;
  status: string;
};

export type FirmwareUpdateJob = {
  id: string;
  machine_id: string;
  status: "submitted" | "running" | "succeeded" | "failed";
  image_url: string;
  component: string;
  apply_time: string;
  targets: string[];
  task_state: string;
  message: string;
  task_monitor: string;
  requested_by: string;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
};

export type MachineFirmware = {
  machine_id: string;
  bmc_configured: boolean;
  detail?: string;
  service_enabled?: boolean;
  service_state?: string;
  firmware?: FirmwareInventoryItem[];
  jobs: FirmwareUpdateJob[];
};

function machineFirmwarePath(tenantId: string, projectId: string, machineId: string): string {
  return (
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId +
    "/fleet/machines/" + machineId + "/firmware/"
  );
}

export function getMachineFirmware(
  tenantId: string,
  projectId: string,
  machineId: string,
): Promise<MachineFirmware> {
  return request(machineFirmwarePath(tenantId, projectId, machineId));
}

export function submitFirmwareUpgrade(
  tenantId: string,
  projectId: string,
  machineId: string,
  input: {
    image_url: string;
    component?: string;
    apply_time?: string;
    targets?: string[];
  },
): Promise<FirmwareUpdateJob> {
  return request(machineFirmwarePath(tenantId, projectId, machineId) + "upgrades/", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ ...input, confirm: true }),
  });
}

export function getFirmwareUpdateJob(
  tenantId: string,
  projectId: string,
  machineId: string,
  jobId: string,
): Promise<FirmwareUpdateJob> {
  return request(
    machineFirmwarePath(tenantId, projectId, machineId) + "upgrades/" + jobId + "/",
  );
}

export type EnrolledAgentNode = AgentNode & {
  enrollment_token: string;
  token_warning: string;
};

export type AgentCommand = {
  id: string;
  node_id: string;
  deployment_id: string | null;
  command_type: string;
  payload: Record<string, unknown>;
  status:
    | "waiting_approval"
    | "queued"
    | "running"
    | "succeeded"
    | "failed"
    | "cancelled"
    | "expired";
  plan_hash: string;
  approved_at: string | null;
  attempts: number;
  result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export function getAgentNodes(
  tenantId: string,
  projectId: string,
): Promise<{ count: number; results: AgentNode[] }> {
  return request("/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/agent-nodes/");
}

export function enrollAgentNode(
  tenantId: string,
  projectId: string,
  payload: {
    name: string;
    node_type: AgentNode["node_type"];
    cluster_name?: string;
    capabilities?: string[];
    labels?: Record<string, string>;
  },
): Promise<EnrolledAgentNode> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/agent-nodes/",
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function revokeAgentNode(nodeId: string): Promise<AgentNode> {
  return request("/api/v1/agent-nodes/" + nodeId + "/revoke/", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function getAgentCommands(
  nodeId: string,
): Promise<{ count: number; results: AgentCommand[] }> {
  return request("/api/v1/agent-nodes/" + nodeId + "/commands/");
}

export function getProjectAgentCommands(
  tenantId: string,
  projectId: string,
  options: {
    limit?: number;
    offset?: number;
    status?: string;
    nodeId?: string;
    commandType?: string;
  } = {},
): Promise<{ count: number; limit: number; offset: number; results: AgentCommand[] }> {
  const query = new URLSearchParams({
    limit: String(options.limit ?? 100),
    offset: String(options.offset ?? 0),
  });
  if (options.status) query.set("status", options.status);
  if (options.nodeId) query.set("node_id", options.nodeId);
  if (options.commandType) query.set("command_type", options.commandType);
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/agent-commands/?" + query.toString(),
  );
}

export const SUPPORTED_KUBERNETES_VERSIONS = ["v1.33", "v1.34", "v1.35", "v1.36"] as const;

export function createAgentCommand(
  nodeId: string,
  commandType: string,
  payload: Record<string, unknown>,
): Promise<AgentCommand> {
  return request("/api/v1/agent-nodes/" + nodeId + "/commands/", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ command_type: commandType, payload }),
  });
}

export function approveAgentCommand(
  commandId: string,
  planHash: string,
): Promise<AgentCommand> {
  return request("/api/v1/agent-commands/" + commandId + "/approve/", {
    method: "POST",
    body: JSON.stringify({ plan_hash: planHash }),
  });
}


export type NetworkSpecStatus = {
  version: number;
  current_version: number;
  migration: {
    code: string;
    version: number;
    current_version: number;
    detail: string;
  } | null;
};

export type ClusterDeployment = {
  id: string;
  tenant_id: string;
  project_id: string;
  cluster_name: string;
  kubernetes_version: string;
  eph_node_id: string;
  target_node_id: string | null;
  status: string;
  spec: Record<string, unknown>;
  network_spec_status?: NetworkSpecStatus;
  plan_hash: string;
  plan_result: Record<string, unknown> | null;
  preflight_result?: {
    ironic_workload?: {
      configured: boolean;
      warnings: string[];
      defaults_sources?: string[];
    };
    network_readiness?: NetworkReadinessReport | null;
    network_defaults?: {
      sriov_pci_sources: Record<
        string,
        {
          origin: string;
          interfaces: string[];
          pci_addresses: string[];
        }
      >;
      warnings: string[];
      eph_network_configured?: boolean;
      bmc_network?: {
        subnets: string[];
        ok: boolean;
        warnings: string[];
        machines: Array<{
          name: string;
          bmc_host: string;
          code: string;
          detail: string;
        }>;
      };
    };
    eph_preflight?: EphPreflightRecord | null;
    bmh_inventory_sync?: BmhInventorySyncRecord | null;
  } | null;
  error: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  network_evidence_status?:
    | "missing"
    | { status: "current" | "drift"; drift: Array<Record<string, unknown>> };
};

export type EphPreflightRecord = {
  available: boolean;
  reason: string | null;
  status: string | null;
  error_code: string | null;
  eph_ready: boolean | null;
  warnings: string[];
  observed_at: string | null;
  command_id: string | null;
  synced_at: string;
};

export type BmhInventorySyncRecord = {
  available: boolean;
  reason: string | null;
  status: string | null;
  error_code: string | null;
  command_id: string | null;
  host_count: number;
  matched: number;
  unmatched: string[];
  warnings: string[];
  observed_at: string | null;
  synced_at: string;
};

export function syncClusterDeploymentBmhInventory(
  deploymentId: string,
): Promise<{
  bmh_inventory: BmhInventorySyncRecord;
  updated?: Array<Record<string, unknown>>;
}> {
  return request(
    "/api/v1/cluster-deployments/" + deploymentId + "/bmh-inventory/sync/",
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function syncClusterDeploymentEphPreflight(
  deploymentId: string,
): Promise<{
  eph_preflight: EphPreflightRecord;
  preflight_result: ClusterDeployment["preflight_result"];
}> {
  return request(
    "/api/v1/cluster-deployments/" + deploymentId + "/preflight/eph/sync/",
    { method: "POST", body: JSON.stringify({}) },
  );
}

export type EtcdBackupCommandSummary = {
  command_id: string;
  status: string;
  created_at: string | null;
  finished_at: string | null;
  backup: { name?: string; path?: string; verified?: boolean; taken_at?: string } | null;
  pruned: string[];
  pruned_entries?: Array<{ name: string; directory: string }>;
  count: number | null;
  failure_code: string | null;
  error: unknown;
};

export type EtcdRestorePoint = {
  name: string;
  kind: "periodic" | "pre_upgrade";
  path: string;
  listed: boolean;
  unconfirmed?: boolean;
  reason?: string | null;
  exists?: boolean | null;
  taken_at: string;
  age_hours: number;
  size_bytes: number | null;
  version: string | null;
};

export type EtcdUnconfirmedRestorePoint = {
  name: string;
  path: string | null;
  listed: boolean;
  unconfirmed: boolean;
  reason: string | null;
  exists: boolean | null;
};

export type EtcdBackupState = {
  deployment_id: string;
  cluster_name: string;
  policy:
    | {
        enabled: boolean;
        valid: true;
        directory: string;
        keep_count: number;
        keep_max_age_hours: number;
        pre_upgrade_keep_max_age_hours: number;
        max_age_hours: number;
        min_free_bytes: number;
        protect: string[];
      }
    | { enabled: boolean; valid: false; error: { code: string; message: string } };
  last_run: EtcdBackupCommandSummary | null;
  history: EtcdBackupCommandSummary[];
  restore_point: EtcdRestorePoint | null;
  unconfirmed_restore_points?: EtcdUnconfirmedRestorePoint[];
  directories?: string[];
  invalid_directories?: Array<{ directory: string; reason: string }>;
  reported_by?: string;
  reported_at?: string | null;
  enabled?: boolean;
  last_run_failed?: string | null;
  run_stuck_hours?: number | null;
  scheduler_last_run?: string | null;
  scheduler_age_hours?: number | null;
  newest: { name: string; taken_at?: string } | null;
  newest_age_hours: number | null;
  stale: boolean;
  ok: boolean;
  count?: number;
  prunable?: string[];
  pre_upgrade_count?: number;
  pre_upgrade?: Array<{ name: string; version?: string; taken_at?: string }>;
  findings: Array<{ name: string; ok: boolean; code: string; detail: string }>;
};

export type EtcdBackupPolicyInput = {
  keep_count?: number;
  keep_max_age_hours?: number;
  pre_upgrade_keep_max_age_hours?: number;
  max_age_hours?: number;
  min_free_bytes?: number;
  directory?: string;
  enabled?: boolean;
};

export type EtcdBackupRunResult = {
  deployment_id: string;
  command_id: string;
  status: string;
  created: boolean;
  code: string;
};

export function checkClusterDeploymentEtcdBackup(
  deploymentId: string,
): Promise<EtcdBackupRunResult> {
  return request(
    "/api/v1/cluster-deployments/" + deploymentId + "/etcd-backup/check/",
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function runClusterDeploymentEtcdBackup(
  deploymentId: string,
): Promise<EtcdBackupRunResult> {
  return request(
    "/api/v1/cluster-deployments/" + deploymentId + "/etcd-backup/run/",
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function setClusterDeploymentEtcdBackupPolicy(
  deploymentId: string,
  policy: EtcdBackupPolicyInput,
): Promise<EtcdBackupState> {
  return request(
    "/api/v1/cluster-deployments/" + deploymentId + "/etcd-backup/policy/",
    { method: "POST", body: JSON.stringify(policy) },
  );
}

export function getClusterDeploymentEtcdBackup(
  deploymentId: string,
): Promise<EtcdBackupState> {
  return request("/api/v1/cluster-deployments/" + deploymentId + "/etcd-backup/");
}

export function getClusterDeployments(
  tenantId: string,
  projectId: string,
): Promise<{ count: number; results: ClusterDeployment[] }> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/cluster-deployments/",
  );
}

export type NetworkReadinessReport = {
  required_control_plane: number;
  required_worker: number;
  control_plane_ready: number;
  worker_ready: number;
  observed_machines: number;
  ready: boolean;
  required_sriov_workers: number;
  sriov_ready: number;
  sriov_findings: Array<{
    machine_id: string;
    name: string;
    policy: string;
    code: string;
    pci_address: string;
    expected?: unknown;
    observed?: unknown;
  }>;
  machines_with_findings: Array<{
    machine_id: string;
    name: string;
    roles?: Record<
      string,
      {
        ok: boolean;
        checked_networks: string[];
        findings: Array<{
          code: string;
          status: string;
          network: string | null;
          interface: string | null;
          expected: unknown;
          observed: unknown;
        }>;
      }
    >;
  }>;
  blocked_machines: Array<{
    machine_id: string;
    name: string;
    reason: string;
  }>;
};

export type InfraNetwork = {
  name: string;
  role: "control" | "data" | "storage-internal" | "storage-external" | "sriov" | "provisioning";
  enabled?: boolean;
  vlan?: number | null;
  mtu?: number;
  dhcp?: boolean;
  default_route?: boolean;
  cidr?: string | null;
  gateway_ipv4?: string | null;
  ip_pools?: Array<{ start: string; end: string; prefix: number }>;
  interface?: string | null;
  bond?: { name: string; interfaces: string[]; mode?: string } | null;
  pci_addresses?: string[];
  num_vfs?: number;
  host_roles: Array<"control_plane" | "worker" | "storage">;
};

export type InfraNetworkProfile = {
  networks: InfraNetwork[];
  dns_servers?: string[];
};

export type InfraNetworkGenerated = {
  networks: InfraNetwork[];
  dns_servers: string[];
  role_network_data: Record<string, Record<string, unknown>>;
  ip_pools: Array<Record<string, unknown>>;
};

export function getInfraNetworkProfile(
  tenantId: string,
  projectId: string,
): Promise<{ profile: InfraNetworkProfile | Record<string, never>; generated: InfraNetworkGenerated | null }> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/infrastructure-networks/",
  );
}

//: One declared port checked against the inventories the machines reported.
export type PortInventoryFinding = {
  port: string;
  matched: boolean;
  hosts: string[];
  missing_on: string[];
  code: string;
  detail: string;
};

export type PortInventoryReport = {
  ok: boolean;
  ports: PortInventoryFinding[];
  warnings: string[];
  hosts: number;
};

export function saveInfraNetworkProfile(
  tenantId: string,
  projectId: string,
  profile: InfraNetworkProfile,
): Promise<{
  profile: InfraNetworkProfile;
  generated: InfraNetworkGenerated;
  port_inventory?: PortInventoryReport;
  warnings?: string[];
}> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/infrastructure-networks/",
    {
      method: "PUT",
      body: JSON.stringify({ profile }),
    },
  );
}

export type IronicTemplatePreview = {
  configuration: {
    namespace: string;
    api_credentials_name: string;
    version: string;
    images: {
      ironic: string;
      deploy_ramdisk_downloader: string;
      keepalived: string;
    };
    networking: { interface?: string; ip_address?: string; dhcp?: boolean };
  };
  provisioning_network: string | null;
  defaults_sources: string[];
  manual_fields: string[];
  warnings: string[];
};

export function previewIronicTemplate(
  tenantId: string,
  projectId: string,
  profile: InfraNetworkProfile | null,
  ephNetworkAddresses?: Record<string, string>,
): Promise<IronicTemplatePreview> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/cluster-deployments/ironic-template/",
    {
      method: "POST",
      body: JSON.stringify({
        ...(profile ? { infra_network_profile: profile } : {}),
        ...(ephNetworkAddresses ? { eph_network_addresses: ephNetworkAddresses } : {}),
      }),
    },
  );
}

export type LabWorkflowCheck = { name: string; ok: boolean; detail: string };

export type LabWorkflowRun = {
  status: string | null;
  conclusion: string | null;
  run_number: number | null;
  event: string | null;
  created_at: string | null;
  updated_at: string | null;
  html_url: string | null;
};

export type LabWorkflowReport = {
  available: boolean;
  workflow: string;
  checks: LabWorkflowCheck[];
  dispatch_inputs: string[];
  secrets_referenced: string[];
  environment: string | null;
  ready: boolean;
  // "observed" only when a GitHub token proved the workflow actually ran.
  secret_presence: "not_observable" | "observed";
  last_run: LabWorkflowRun | null;
  reason: string | null;
  workflow_url: string | null;
  readiness_job?: { name: string | null; runs_on: string[]; environment: string | null; outputs: string[] };
  acceptance_job?: { name: string | null; runs_on: string[]; needs: string | null; environment: string | null };
};

export function getLabWorkflow(): Promise<LabWorkflowReport> {
  return request("/api/v1/metal3/lab-workflow/");
}

export function createClusterDeployment(
  tenantId: string,
  projectId: string,
  ephNodeId: string,
  clusterSpec: Record<string, unknown>,
): Promise<ClusterDeployment> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/cluster-deployments/",
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({
        eph_node_id: ephNodeId,
        cluster_spec: clusterSpec,
      }),
    },
  );
}

export function updateClusterSriovDevicePlugin(
  deploymentId: string,
  pools: Record<string, unknown>,
): Promise<ClusterDeployment> {
  return request("/api/v1/cluster-deployments/" + deploymentId + "/sriov-device-plugin/", {
    method: "PUT",
    body: JSON.stringify({ pools }),
  });
}

export function cancelClusterDeployment(deploymentId: string): Promise<ClusterDeployment> {
  return request("/api/v1/cluster-deployments/" + deploymentId + "/cancel/", {
    method: "POST",
    body: JSON.stringify({}),
  });
}


export type KnowledgeBase = {
  id: string;
  tenant_id: string;
  project_id: string;
  name: string;
  slug: string;
  description: string;
  embedding_model_id: string;
  chunk_size: number;
  chunk_overlap: number;
  is_archived: boolean;
  document_count: number;
  created_at: string;
  updated_at: string;
};

export type KnowledgeDocument = {
  id: string;
  knowledge_base_id: string;
  title: string;
  metadata: Record<string, unknown>;
  checksum: string;
  status: "pending" | "ingesting" | "ready" | "failed";
  chunk_count: number;
  error: Record<string, unknown> | null;
  ingested_at: string | null;
  created_at: string;
  updated_at: string;
};

export function getKnowledgeBases(
  tenantId: string,
  projectId: string,
): Promise<Page<KnowledgeBase>> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/knowledge-bases/",
  );
}

export function createKnowledgeBase(
  tenantId: string,
  projectId: string,
  payload: {
    name: string;
    slug: string;
    description: string;
    embedding_model_id: string;
    chunk_size: number;
    chunk_overlap: number;
  },
): Promise<KnowledgeBase> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/knowledge-bases/",
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function getKnowledgeDocuments(
  knowledgeBaseId: string,
): Promise<Page<KnowledgeDocument>> {
  return request("/api/v1/knowledge-bases/" + knowledgeBaseId + "/documents/");
}

export function createKnowledgeDocument(
  knowledgeBaseId: string,
  title: string,
  content: string,
  metadata: Record<string, unknown> = {},
): Promise<KnowledgeDocument> {
  return request("/api/v1/knowledge-bases/" + knowledgeBaseId + "/documents/", {
    method: "POST",
    body: JSON.stringify({ title, content, metadata }),
  });
}

export function deleteKnowledgeDocument(documentId: string): Promise<void> {
  return request("/api/v1/knowledge-documents/" + documentId + "/", {
    method: "DELETE",
  });
}


export function createOpenAICompatibleProvider(
  tenantId: string,
  name: string,
  baseUrl: string,
  credentialRef: string,
): Promise<ModelProvider> {
  return request<ModelProvider>(`/api/v1/tenants/${tenantId}/model-providers/`, {
    method: "POST",
    body: JSON.stringify({
      name,
      provider_type: "openai_compatible",
      base_url: baseUrl,
      credential_ref: credentialRef,
      config: {},
    }),
  });
}


export function runClusterDeploymentPostflight(deploymentId: string, targetNodeId: string) {
  return request(
    "/api/v1/cluster-deployments/" + deploymentId + "/target-postflight/",
    { method: "POST", body: JSON.stringify({ target_node_id: targetNodeId, confirmation: "cluster_provisioned" }) },
  );
}

export type TenantMember = {
  id: string;
  user_id: number;
  username: string;
  email: string;
  role: "owner" | "admin" | "member" | "viewer";
};

export function getTenantMembers(tenantId: string): Promise<TenantMember[]> {
  return request("/api/v1/tenants/" + tenantId + "/members/");
}

export function addTenantMember(
  tenantId: string,
  username: string,
  role: TenantMember["role"],
): Promise<TenantMember> {
  return request("/api/v1/tenants/" + tenantId + "/members/", {
    method: "POST",
    body: JSON.stringify({ username, role }),
  });
}

export function updateTenantMember(
  tenantId: string,
  membershipId: string,
  role: TenantMember["role"],
): Promise<TenantMember> {
  return request(
    "/api/v1/tenants/" + tenantId + "/members/" + membershipId + "/",
    { method: "PATCH", body: JSON.stringify({ role }) },
  );
}

export function removeTenantMember(tenantId: string, membershipId: string): Promise<void> {
  return request(
    "/api/v1/tenants/" + tenantId + "/members/" + membershipId + "/",
    { method: "DELETE" },
  );
}

export type AuditEvent = {
  id: string;
  tenant_id: string;
  project_id: string | null;
  actor_id: number | null;
  actor_username: string | null;
  event_type: string;
  resource_type: string;
  resource_id: string;
  outcome: "success" | "denied" | "failure";
  request_id: string;
  remote_addr: string | null;
  user_agent: string;
  metadata: Record<string, unknown>;
  previous_hash: string;
  event_hash: string;
  created_at: string;
};

export type AuditChainVerification = {
  valid: boolean;
  verified_count: number;
  invalid_event_id?: string | null;
  expected_previous_hash?: string;
  stored_previous_hash?: string;
  head_hash?: string;
};

export function getAuditEvents(
  tenantId: string,
  projectId?: string,
): Promise<{ count: number; limit: number; offset: number; results: AuditEvent[] }> {
  const query = new URLSearchParams({ limit: "100", offset: "0" });
  if (projectId) query.set("project_id", projectId);
  return request(
    "/api/v1/tenants/" + tenantId + "/audit-events/?" + query.toString(),
  );
}

export function verifyAuditChain(tenantId: string): Promise<AuditChainVerification> {
  return request("/api/v1/tenants/" + tenantId + "/audit-events/verify/");
}


export function retryKnowledgeDocument(documentId: string): Promise<KnowledgeDocument> {
  return request("/api/v1/knowledge-documents/" + documentId + "/retry/", {
    method: "POST",
  });
}

export type AgentPluginProtocol =
  | "http_json"
  | "mcp"
  | "a2a"
  | "openai_compatible"
  | "open_data";

export type AgentPluginInstallationStatus = "installed" | "disabled" | "uninstalled";

export type AgentPluginManifestCapability = {
  name: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  risk_level?: string;
  timeout_seconds?: number;
};

export type AgentPluginDefinition = {
  id: string;
  tenant_id: string;
  plugin_key: string;
  name: string;
  description: string;
  version: string;
  protocol: AgentPluginProtocol;
  endpoint_url: string;
  auth_required: boolean;
  credential_configured?: boolean;
  capabilities: string[];
  capability_definitions?: AgentPluginManifestCapability[];
  input_schema: Record<string, unknown> | null;
  output_schema: Record<string, unknown> | null;
  timeout_seconds: number | null;
  max_concurrency: number | null;
  enabled: boolean;
  schema_fingerprint?: string;
  created_at: string;
  updated_at: string;
  manifest?: {
    capabilities?: AgentPluginManifestCapability[];
    catalog?: {
      kind?: string;
      category?: string;
      vendor?: string;
      availability?: "builtin" | "ready" | "unprovisioned";
      usage_count?: number;
      featured?: boolean;
    };
  } | null;
};

export type AgentPluginInstallation = {
  id: string;
  tenant_id: string;
  project_id: string;
  plugin: AgentPluginDefinition | null;
  plugin_key?: string;
  plugin_version: string;
  status: AgentPluginInstallationStatus;
  granted_capabilities: string[];
  credential_configured: boolean;
  created_at: string;
  updated_at: string;
};

export type AgentInvocationRecord = {
  status: "success" | "failed" | "rejected";
  error_code: string;
  created_at: string;
};

export type InstalledAgentCapability = {
  name: string;
  plugin_key: string;
  plugin_version: string;
  protocol: string;
  risk_level: string;
  installation_id: string;
  input_schema: Record<string, unknown>;
  last_invocation: AgentInvocationRecord | null;
};

export type AgentCapabilityResponse = {
  capabilities: InstalledAgentCapability[];
};

export function getAgentPluginDefinitions(tenantId: string): Promise<AgentPluginDefinition[]> {
  return request(
    "/api/v1/tenants/" + tenantId + "/agent-plugins/",
  );
}

export type AgentPluginStudioRequest = {
  plugin_type: "openapi" | "mcp" | "http_api";
  base_url: string;
  document_url?: string;
  document?: Record<string, unknown>;
  auth_type: "none" | "bearer" | "api_key" | "oauth2";
  auth_config?: Record<string, string>;
  credential?: string;
};

export type AgentPluginStudioCapability = AgentPluginManifestCapability & {
  source?: string;
  remote_operation?: string;
  remote_name?: string;
};

export type AgentPluginStudioDraft = {
  protocol: AgentPluginProtocol;
  endpoint_url: string;
  manifest: Record<string, unknown> & {
    capabilities?: AgentPluginStudioCapability[];
    permission_policy?: {
      approval_required?: string[];
      blocked_methods?: string[];
      max_risk?: string;
    };
  };
  capabilities: AgentPluginStudioCapability[];
  skipped: { method: string; path: string; reason: string }[];
  connection: {
    status: string;
    http_status: number;
    authenticated: boolean;
    message: string;
  };
  auth_mode: string;
  credential_kind: string;
  credential_configured: boolean;
  schema_fingerprint: string;
  generated: Record<string, unknown>;
};

/** Discover a real service and generate its schemas without saving anything. */
export function discoverAgentPlugin(
  tenantId: string,
  payload: AgentPluginStudioRequest,
): Promise<AgentPluginStudioDraft> {
  return request("/api/v1/tenants/" + tenantId + "/agent-plugins/studio/discover/", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(payload),
  });
}

/** Re-run discovery as an explicit connection and authentication test. */
export function testAgentPluginStudio(
  tenantId: string,
  payload: AgentPluginStudioRequest,
): Promise<{ status: string; connection: AgentPluginStudioDraft["connection"]; tool_count: number }> {
  return request("/api/v1/tenants/" + tenantId + "/agent-plugins/studio/test/", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export type AgentPluginStudioPublishResult = {
  definition: AgentPluginDefinition;
  installation: AgentPluginInstallation | null;
  schema_fingerprint: string;
  changed: boolean;
  version: string;
};

/** Publish a discovered plugin and auto-install it into the current project. */
export function publishAgentPlugin(
  tenantId: string,
  payload: AgentPluginStudioRequest & {
    plugin_key: string;
    name: string;
    description?: string;
    version?: string;
    project_id?: string;
    selected_capabilities?: string[];
  },
): Promise<AgentPluginStudioPublishResult> {
  return request("/api/v1/tenants/" + tenantId + "/agent-plugins/studio/publish/", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(payload),
  });
}

/** Register a new connector definition owned by the tenant. */
export function createAgentPlugin(
  tenantId: string,
  payload: {
    plugin_key: string;
    name: string;
    description: string;
    version: string;
    protocol: AgentPluginProtocol;
    endpoint_url: string;
    auth_required: boolean;
    capabilities: string[];
  },
): Promise<AgentPluginDefinition> {
  return request("/api/v1/tenants/" + tenantId + "/agent-plugins/", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(payload),
  });
}

/** Turn a connector definition on or off for the whole tenant. */
export type AgentCapabilityInvokeResult = {
  status: "success";
  capability: string;
  plugin_key: string;
  latency_ms: number;
  result: Record<string, unknown>;
};

/** Run one declared capability of a connector installed in this project. */
export function invokeAgentPluginCapability(
  tenantId: string,
  projectId: string,
  pluginId: string,
  capability: string,
  args: Record<string, unknown>,
): Promise<AgentCapabilityInvokeResult> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/agent-plugins/" +
      pluginId + "/capabilities/" + encodeURIComponent(capability) + "/invoke/",
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ arguments: args }),
    },
  );
}

export type AgentPluginVersion = {
  id: string;
  version: string;
  enabled: boolean;
  is_active: boolean;
  schema_fingerprint: string;
  capabilities: number;
  created_at: string;
};

export function getAgentPluginVersions(
  tenantId: string,
  pluginKey: string,
): Promise<AgentPluginVersion[]> {
  return request(
    "/api/v1/tenants/" +
      tenantId +
      "/agent-plugins/versions/?plugin_key=" +
      encodeURIComponent(pluginKey),
  );
}

export function activateAgentPluginVersion(pluginId: string): Promise<AgentPluginDefinition> {
  return request("/api/v1/agent-plugins/" + pluginId + "/activate/", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({}),
  });
}

export function setAgentPluginEnabled(
  pluginId: string,
  enabled: boolean,
): Promise<AgentPluginDefinition> {
  return request("/api/v1/agent-plugins/" + pluginId + "/", {
    method: "POST",
    body: JSON.stringify({ enabled }),
  });
}

export function getAgentPluginInstallations(
  tenantId: string,
  projectId: string,
): Promise<AgentPluginInstallation[]> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/agent-plugin-installations/",
  );
}

export function installAgentPlugin(
  tenantId: string,
  projectId: string,
  pluginId: string,
  body: { granted_capabilities?: string[]; credential_ref?: string } = {},
): Promise<AgentPluginInstallation> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/agent-plugin-installations/",
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ plugin_id: pluginId, ...body }),
    },
  );
}

export type MarketplaceCatalogCapability = {
  name: string;
  description: string;
  risk_level: string;
};

export type MarketplaceCatalogEntry = {
  plugin_key: string;
  name: string;
  description: string;
  version: string;
  protocol: string;
  enabled: boolean;
  auth_required: boolean;
  capabilities: MarketplaceCatalogCapability[];
  installed: boolean;
  installation_status: "installed" | "disabled" | "uninstalled" | "not_installed";
  installation_id: string | null;
  project_id: string | null;
};

export type MarketplaceCatalogResponse = {
  count: number;
  entries: MarketplaceCatalogEntry[];
};

export type MarketplaceCatalogParams = {
  capability?: string;
  protocol?: string;
  installedOnly?: boolean;
};

export function fetchMarketplaceCatalog(
  tenantId: string,
  projectId: string,
  params: MarketplaceCatalogParams = {},
): Promise<MarketplaceCatalogResponse> {
  const query = new URLSearchParams();
  if (params.capability && params.capability.trim()) {
    query.set("capability", params.capability.trim());
  }
  if (params.protocol && params.protocol.trim()) {
    query.set("protocol", params.protocol.trim());
  }
  if (params.installedOnly !== undefined) {
    query.set("installed", params.installedOnly ? "true" : "false");
  }
  if (projectId) query.set("project_id", projectId);
  const suffix = query.toString() ? "?" + query.toString() : "";
  return request(
    "/api/v1/tenants/" + tenantId + "/marketplace/catalog/" + suffix,
  );
}

export function installAgentPluginFromCatalog(
  tenantId: string,
  projectId: string,
  payload: {
    plugin_key: string;
    plugin_version: string;
    granted_capabilities: string[];
    credential_ref?: string;
  },
): Promise<AgentPluginInstallation> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/agent-plugin-installations/",
    {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(payload),
    },
  );
}

export function setAgentPluginInstallationStatus(
  installationId: string,
  status: AgentPluginInstallationStatus,
): Promise<AgentPluginInstallation> {
  return request("/api/v1/agent-plugin-installations/" + installationId + "/status/", {
    method: "POST",
    body: JSON.stringify({ status }),
  });
}

export function rotateAgentPluginCredential(
  installationId: string,
  credentialRef: string,
): Promise<AgentPluginInstallation> {
  return request("/api/v1/agent-plugin-installations/" + installationId + "/credential/", {
    method: "POST",
    body: JSON.stringify({ credential_ref: credentialRef }),
  });
}

export function getProjectAgentCapabilities(
  tenantId: string,
  projectId: string,
): Promise<AgentCapabilityResponse> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/agent-capabilities/",
  );
}


export function getProviderCatalog(tenantId: string): Promise<ProviderCatalog> {
  return request<ProviderCatalog>(
    `/api/v1/tenants/${tenantId}/model-providers/catalog/`,
  );
}

export type ProviderOAuthStart = { authorization_url: string; state: string };

export function startProviderOAuth(
  tenantId: string,
  presetKey: string,
  redirectUri: string,
): Promise<ProviderOAuthStart> {
  return request<ProviderOAuthStart>(
    `/api/v1/tenants/${tenantId}/model-providers/oauth/${presetKey}/start/`,
    { method: "POST", body: JSON.stringify({ redirect_uri: redirectUri }) },
  );
}

export function exchangeProviderOAuth(
  tenantId: string,
  presetKey: string,
  code: string,
  state: string,
): Promise<{ provider: ModelProvider; account: ProviderAccount }> {
  return request<{ provider: ModelProvider; account: ProviderAccount }>(
    `/api/v1/tenants/${tenantId}/model-providers/oauth/${presetKey}/exchange/`,
    { method: "POST", body: JSON.stringify({ code, state }) },
  );
}

export function createAccountProvider(
  tenantId: string,
  presetKey: string,
  name: string,
): Promise<ModelProvider> {
  return request<ModelProvider>(`/api/v1/tenants/${tenantId}/model-providers/`, {
    method: "POST",
    body: JSON.stringify({
      name,
      provider_type: "openai_compatible",
      auth_mode: "user_account",
      preset_key: presetKey,
      config: {},
    }),
  });
}

export function linkProviderAccount(
  tenantId: string,
  providerId: string,
  credentialRef: string,
  accountLabel = "",
): Promise<ProviderAccount> {
  return request<ProviderAccount>(
    `/api/v1/tenants/${tenantId}/model-providers/${providerId}/account/`,
    {
      method: "POST",
      body: JSON.stringify({ credential_ref: credentialRef, account_label: accountLabel }),
    },
  );
}

export function unlinkProviderAccount(
  tenantId: string,
  providerId: string,
): Promise<ProviderAccount> {
  return request<ProviderAccount>(
    `/api/v1/tenants/${tenantId}/model-providers/${providerId}/account/`,
    { method: "DELETE" },
  );
}

export type ChatContextItem = { id: string; name: string; slug?: string; installation_id?: string };
export type ChatModelOption = {
  id: string;
  recommended?: boolean;
  model_name: string;
  display_name: string;
  provider_name: string;
  provider_type: string;
  auth_mode: "tenant_secret" | "user_account" | "platform_pool";
  account: { linked: boolean; remaining_today: number } | null;
  ready?: boolean;
};
export type ChatCatalogEntry = {
  key: string;
  name: string;
  description: string;
  protocol: string;
  /** The connector endpoint is configured, so the composer can install it. */
  enabled: boolean;
  auth_required: boolean;
  capabilities: string[];
};

export type ChatContext = {
  project: ChatContextItem;
  projects: ChatContextItem[];
  skills: ChatContextItem[];
  catalog?: ChatCatalogEntry[];
  plugins: (ChatContextItem & {
    key: string;
    version: string;
    protocol: string;
    /** The plugin connector cannot run until a credential reference is bound. */
    auth_required?: boolean;
    /** True once the installation points at a credential reference. */
    credential_configured?: boolean;
  })[];
  models: ChatModelOption[];
};
export type ChatToolCall = {
  name: string;
  identifier: string;
  kind: "platform" | "plugin" | string;
  arguments: Record<string, unknown>;
  ok: boolean;
  code: string | null;
  duration_ms: number;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  tool_calls?: ChatToolCall[];
  context: {
    skills?: string[];
    plugins?: string[];
    attachments?: string[];
    knowledge_documents?: string[];
  };
  usage: Record<string, unknown>;
  error_code?: string;
  created_at: string;
  author?: string | null;
};
export type ChatThread = {
  id: string;
  title: string;
  project_id: string;
  model_config_id: string | null;
  archived: boolean;
  execution_target: "cloud" | "local";
  effort: "auto" | "low";
  goal: string;
  plan_first: boolean;
  created_at: string;
  updated_at: string;
  last_message?: { role: string; content: string; created_at: string };
};

export function installChatPlugin(
  tenantId: string,
  projectId: string,
  pluginKey: string,
  capabilities: string[],
): Promise<{ id: string }> {
  return request<{ id: string }>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/agent-plugin-installations/`,
    {
      method: "POST",
      body: JSON.stringify({ plugin_key: pluginKey, granted_capabilities: capabilities }),
    },
  );
}

export function getChatContext(tenantId: string, projectId: string): Promise<ChatContext> {
  return request<ChatContext>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/agent-chat/context/`,
  );
}

export function getChatThreads(tenantId: string, projectId: string): Promise<{ items: ChatThread[] }> {
  return request<{ items: ChatThread[] }>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/agent-chat/threads/`,
  );
}

export function createChatThread(
  tenantId: string,
  projectId: string,
  payload: { title?: string; model_config_id?: string | null } = {},
): Promise<ChatThread> {
  return request<ChatThread>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/agent-chat/threads/`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function updateChatThread(
  threadId: string,
  payload: {
    title?: string;
    archived?: boolean;
    model_config_id?: string | null;
    execution_target?: "cloud" | "local";
    effort?: "auto" | "low";
    goal?: string;
    plan_first?: boolean;
  },
): Promise<ChatThread> {
  return request<ChatThread>(`/api/v1/agent-chat/threads/${threadId}/`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function getChatThread(
  threadId: string,
): Promise<ChatThread & { messages: ChatMessage[]; attachments?: ChatAttachment[] }> {
  return request<ChatThread & { messages: ChatMessage[]; attachments?: ChatAttachment[] }>(
    `/api/v1/agent-chat/threads/${threadId}/`,
  );
}

export function deleteChatThread(threadId: string): Promise<void> {
  return request<void>(`/api/v1/agent-chat/threads/${threadId}/`, { method: "DELETE" });
}

export type ChatAttachment = {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
};

export function sendChatMessage(
  threadId: string,
  payload: {
    content: string;
    model_config_id?: string | null;
    context?: {
      skills?: string[];
      plugins?: string[];
      attachments?: string[];
      knowledge_documents?: string[];
    };
  },
  options: { signal?: AbortSignal } = {},
): Promise<{ user: ChatMessage; assistant: ChatMessage }> {
  return request<{ user: ChatMessage; assistant: ChatMessage }>(
    `/api/v1/agent-chat/threads/${threadId}/messages/`,
    { method: "POST", body: JSON.stringify(payload), signal: options.signal },
  );
}

export function uploadChatAttachment(
  threadId: string,
  file: File,
): Promise<ChatAttachment> {
  const body = new FormData();
  body.append("file", file);
  return request<ChatAttachment>(`/api/v1/agent-chat/threads/${threadId}/attachments/`, {
    method: "POST",
    body,
  });
}

export function getChatAttachments(
  threadId: string,
): Promise<{ items: ChatAttachment[] }> {
  return request(`/api/v1/agent-chat/threads/${threadId}/attachments/`);
}

export function deleteChatAttachment(attachmentId: string): Promise<void> {
  return request(`/api/v1/agent-chat/attachments/${attachmentId}/`, { method: "DELETE" });
}

export function chatAttachmentUrl(attachmentId: string): string {
  return `/api/v1/agent-chat/attachments/${attachmentId}/`;
}


export type RoadmapItem = {
  code: string;
  title: string;
  detail: string;
  status: "verified" | "partial" | "in_progress";
  evidence: Record<string, number>;
};

export function getPlatformRoadmap(): Promise<{ items: RoadmapItem[] }> {
  return request<{ items: RoadmapItem[] }>("/api/v1/platform/roadmap/");
}

// ---------------------------------------------------------------------------
// Central container image registry
//
// The catalog is the only place an agent or cluster learns where an image
// lives; deployments consume digest-pinned internal references from here.
// ---------------------------------------------------------------------------
export type RegistryCategory =
  | "platform"
  | "agent"
  | "infrastructure"
  | "kubernetes"
  | "network"
  | "observability"
  | "workload";

export type RegistryImage = {
  id: string;
  name: string;
  category: RegistryCategory;
  source_repository: string;
  build_ref: string;
  digest: string;
  internal_ref: string;
  size_bytes: number;
  status: "building" | "scanning" | "ready" | "blocked" | "retired";
  stage: "dev" | "staging" | "production";
  scan_status: "pending" | "passed" | "failed";
  scan_findings: number;
  sbom_ref: string;
  signed: boolean;
  promoted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type RegistrySummary = {
  total: number;
  by_category: Record<string, number>;
  by_stage: Record<string, number>;
  by_status: Record<string, number>;
  signed: number;
  scan_passed: number;
  registry_host: string;
};

export type RegistryPullPlan = {
  registry_host: string;
  digest_pinned: boolean;
  images: {
    name: string;
    category: string;
    digest: string;
    internal_ref: string;
    stage: string;
  }[];
};

export type RegistryEvent = {
  id: string;
  action: string;
  actor: string | null;
  detail: Record<string, unknown>;
  created_at: string;
};

function registryBase(tenantId: string, projectId: string): string {
  return "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/registry";
}

export function getRegistryImages(
  tenantId: string,
  projectId: string,
  filters: { category?: string; stage?: string; status?: string } = {},
): Promise<{ images: RegistryImage[] }> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) query.set(key, value);
  }
  const suffix = query.toString() ? "?" + query.toString() : "";
  return request(registryBase(tenantId, projectId) + "/images/" + suffix);
}

export function registerRegistryImage(
  tenantId: string,
  projectId: string,
  payload: {
    name: string;
    digest: string;
    category: RegistryCategory;
    source_repository?: string;
    build_ref?: string;
    size_bytes?: number;
  },
): Promise<RegistryImage> {
  return request(registryBase(tenantId, projectId) + "/images/", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function scanRegistryImage(
  tenantId: string,
  projectId: string,
  imageId: string,
  payload: { scan_status: "passed" | "failed"; findings?: number; sbom_ref?: string },
): Promise<RegistryImage> {
  return request(registryBase(tenantId, projectId) + "/images/" + imageId + "/scan/", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function signRegistryImage(
  tenantId: string,
  projectId: string,
  imageId: string,
): Promise<RegistryImage> {
  return request(registryBase(tenantId, projectId) + "/images/" + imageId + "/sign/", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function promoteRegistryImage(
  tenantId: string,
  projectId: string,
  imageId: string,
  stage: "dev" | "staging" | "production",
): Promise<RegistryImage> {
  return request(registryBase(tenantId, projectId) + "/images/" + imageId + "/promote/", {
    method: "POST",
    body: JSON.stringify({ stage }),
  });
}

export function getRegistrySummary(
  tenantId: string,
  projectId: string,
): Promise<RegistrySummary> {
  return request(registryBase(tenantId, projectId) + "/summary/");
}

export function getRegistryPullPlan(
  tenantId: string,
  projectId: string,
  category?: string,
): Promise<RegistryPullPlan> {
  const suffix = category ? "?category=" + encodeURIComponent(category) : "";
  return request(registryBase(tenantId, projectId) + "/pull-plan/" + suffix);
}

export function getRegistryImageEvents(
  tenantId: string,
  projectId: string,
  imageId: string,
): Promise<{ events: RegistryEvent[] }> {
  return request(registryBase(tenantId, projectId) + "/images/" + imageId + "/events/");
}

// ---------------------------------------------------------------------------
// Recurring Control Agent turns
// ---------------------------------------------------------------------------
export type ScheduledJob = {
  id: string;
  name: string;
  prompt: string;
  cadence: "hourly" | "daily" | "weekly";
  hour: number;
  weekday: number;
  execution_target: "cloud" | "local";
  effort: "auto" | "low";
  status: "active" | "paused";
  enabled: boolean;
  thread_id: string | null;
  model_config_id: string | null;
  next_run_at: string;
  last_run_at: string | null;
  last_error_code: string;
};

function scheduledBase(tenantId: string, projectId: string): string {
  return `/api/v1/tenants/${tenantId}/projects/${projectId}/scheduled-jobs`;
}

export function getScheduledJobs(
  tenantId: string,
  projectId: string,
): Promise<{ items: ScheduledJob[] }> {
  return request(`${scheduledBase(tenantId, projectId)}/`);
}

export function createScheduledJob(
  tenantId: string,
  projectId: string,
  payload: {
    name: string;
    prompt: string;
    cadence: "hourly" | "daily" | "weekly";
    hour: number;
    weekday?: number;
    execution_target?: "cloud" | "local";
    effort?: "auto" | "low";
    model_config_id?: string | null;
  },
): Promise<ScheduledJob> {
  return request(`${scheduledBase(tenantId, projectId)}/`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateScheduledJob(
  tenantId: string,
  projectId: string,
  jobId: string,
  payload: {
    name?: string;
    prompt?: string;
    cadence?: "hourly" | "daily" | "weekly";
    hour?: number;
    weekday?: number;
    enabled?: boolean;
    effort?: "auto" | "low";
    execution_target?: "cloud" | "local";
  },
): Promise<ScheduledJob> {
  return request(`${scheduledBase(tenantId, projectId)}/${jobId}/`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteScheduledJob(
  tenantId: string,
  projectId: string,
  jobId: string,
): Promise<void> {
  return request(`${scheduledBase(tenantId, projectId)}/${jobId}/`, { method: "DELETE" });
}

export function runScheduledJobNow(
  tenantId: string,
  projectId: string,
  jobId: string,
): Promise<ScheduledJob> {
  return request(`${scheduledBase(tenantId, projectId)}/${jobId}/run-now/`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}
