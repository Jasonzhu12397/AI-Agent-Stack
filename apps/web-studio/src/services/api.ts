const apiBase = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export type CurrentUser = {
  id: number;
  username: string;
  email: string;
  is_staff: boolean;
};

export type Tenant = { id: string; name: string; slug: string };
export type Project = { id: string; tenant_id: string; name: string; slug: string };
export type ModelProvider = {
  id: string;
  name: string;
  provider_type: string;
  base_url: string;
  credential_configured: boolean;
  enabled: boolean;
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
  });
  if (!response.ok) throw new Error("Unable to initialize secure session.");
  const result = (await response.json()) as CsrfResponse;
  return result.csrf_token;
}

export class ApiError extends Error {
  data: Record<string, unknown>;

  constructor(message: string, data: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.data = data;
  }
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
    throw new ApiError(
      payload.detail ?? `Request failed with HTTP ${response.status}`,
      payload as Record<string, unknown>,
    );
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
  status: "waiting_approval" | "queued" | "running" | "succeeded" | "failed" | "cancelled";
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
  count: number | null;
  failure_code: string | null;
  error: unknown;
};

export type EtcdRestorePoint = {
  name: string;
  kind: "periodic" | "pre_upgrade";
  path: string;
  listed: boolean;
  taken_at: string;
  age_hours: number;
  size_bytes: number | null;
  version: string | null;
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


export type PluginTool = {
  id: string;
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  required_permissions: string[];
  risk_level: "low" | "medium" | "high" | "critical";
  timeout_seconds: number;
  retry_policy: Record<string, unknown>;
  idempotency_mode: "none" | "read_only" | "idempotent";
  enabled: boolean;
};

export type PluginDefinition = {
  id: string;
  plugin_key: string;
  name: string;
  description: string;
  version: string;
  connector_type: "native" | "mcp" | "http" | "openapi" | "custom";
  maintainer: string;
  license: string;
  source_url: string;
  manifest: Record<string, unknown>;
  review_status: "draft" | "approved" | "rejected";
  enabled: boolean;
  tools: PluginTool[];
  created_at: string;
  updated_at: string;
};

export type PluginConnection = {
  id: string;
  name: string;
  auth_type: "none" | "credential_ref" | "oauth_ref";
  credential_configured: boolean;
  status: "pending" | "healthy" | "error" | "disabled";
  last_health_check_at: string | null;
};

export type PluginInstallation = {
  id: string;
  tenant_id: string;
  project_id: string;
  plugin_id: string;
  plugin_name: string;
  plugin_key: string;
  plugin_version: string;
  status: "installed" | "disabled" | "uninstalled";
  granted_permissions: string[];
  config: Record<string, unknown>;
  connections: PluginConnection[];
  created_at: string;
  updated_at: string;
};

export function getPluginCatalog(
  tenantId: string,
): Promise<{ count: number; results: PluginDefinition[] }> {
  return request("/api/v1/connectors/?tenant_id=" + encodeURIComponent(tenantId));
}

export function registerPlugin(payload: Record<string, unknown>): Promise<PluginDefinition> {
  return request("/api/v1/connectors/", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function approvePlugin(pluginId: string): Promise<PluginDefinition> {
  return request("/api/v1/connectors/" + pluginId + "/approve/", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function getPluginInstallations(
  tenantId: string,
  projectId: string,
): Promise<{ count: number; results: PluginInstallation[] }> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/plugin-installations/",
  );
}

export function installPlugin(
  tenantId: string,
  projectId: string,
  pluginId: string,
  grantedPermissions: string[],
): Promise<PluginInstallation> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/plugin-installations/",
    {
      method: "POST",
      body: JSON.stringify({
        plugin_id: pluginId,
        granted_permissions: grantedPermissions,
        config: {},
      }),
    },
  );
}

export function setPluginInstallationStatus(
  installationId: string,
  status: "installed" | "disabled" | "uninstalled",
): Promise<PluginInstallation> {
  return request("/api/v1/plugin-installations/" + installationId + "/status/", {
    method: "POST",
    body: JSON.stringify({ status }),
  });
}

export function createPluginConnection(
  installationId: string,
  payload: {
    name: string;
    auth_type: "none" | "credential_ref" | "oauth_ref";
    credential_ref?: string;
  },
): Promise<PluginConnection> {
  return request("/api/v1/plugin-installations/" + installationId + "/connections/", {
    method: "POST",
    body: JSON.stringify({ ...payload, config: {} }),
  });
}

export function testPluginConnection(connectionId: string): Promise<PluginInstallation> {
  return request(
    "/api/v1/plugin-connections/" + connectionId + "/test/",
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function setPluginConnectionStatus(
  connectionId: string,
  status: "pending" | "disabled",
): Promise<PluginInstallation> {
  return request(
    "/api/v1/plugin-connections/" + connectionId + "/status/",
    { method: "POST", body: JSON.stringify({ status }) },
  );
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


export type ToolApprovalRequest = {
  id: string;
  task_id: string;
  tenant_id: string;
  project_id: string;
  tool: string;
  plugin: string;
  plugin_version: string;
  installation_id: string;
  connection_id: string | null;
  call_id: string;
  arguments: Record<string, unknown>;
  arguments_hash: string;
  status: string;
  requested_at: string;
  expires_at: string;
  approved_at: string | null;
  consumed_at: string | null;
  approved_by_id: number | null;
  rejected_by_id: number | null;
  rejected_at: string | null;
  rejection_reason: string;
};

export function getToolApprovals(
  tenantId: string,
  projectId: string,
): Promise<{ items: ToolApprovalRequest[]; count: number }> {
  return request<{ items: ToolApprovalRequest[]; count: number }>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/tool-approvals/?status=pending`,
  );
}

export function approveToolApproval(
  tenantId: string,
  projectId: string,
  approvalId: string,
): Promise<{ approval: ToolApprovalRequest; task_status: string }> {
  return request<{ approval: ToolApprovalRequest; task_status: string }>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/tool-approvals/${approvalId}/approve/`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function rejectToolApproval(
  tenantId: string,
  projectId: string,
  approvalId: string,
  reason: string,
): Promise<{ approval: ToolApprovalRequest; task_status: string }> {
  return request<{ approval: ToolApprovalRequest; task_status: string }>(
    `/api/v1/tenants/${tenantId}/projects/${projectId}/tool-approvals/${approvalId}/reject/`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );
}

export function retryKnowledgeDocument(documentId: string): Promise<KnowledgeDocument> {
  return request("/api/v1/knowledge-documents/" + documentId + "/retry/", {
    method: "POST",
  });
}
