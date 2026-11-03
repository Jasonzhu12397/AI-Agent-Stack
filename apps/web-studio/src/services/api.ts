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

export function publishWorkflow(workflowId: string): Promise<WorkflowVersion> {
  return request<WorkflowVersion>("/api/v1/workflows/" + workflowId + "/publish/", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function getWorkflowRuns(workflowId: string): Promise<WorkflowRun[]> {
  return request<WorkflowRun[]>("/api/v1/workflows/" + workflowId + "/runs/");
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

export type EnrolledAgentNode = AgentNode & {
  enrollment_token: string;
  token_warning: string;
};

export type AgentCommand = {
  id: string;
  node_id: string;
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
  plan_hash: string;
  preflight_result: Record<string, unknown> | null;
  plan_result: Record<string, unknown> | null;
  error: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export function getClusterDeployments(
  tenantId: string,
  projectId: string,
): Promise<{ count: number; results: ClusterDeployment[] }> {
  return request(
    "/api/v1/tenants/" + tenantId + "/projects/" + projectId + "/cluster-deployments/",
  );
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
  credential_ref: string;
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
