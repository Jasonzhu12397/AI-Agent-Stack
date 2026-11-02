<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  approveAgentCommand,
  cancelClusterDeployment,
  createAgentCommand,
  createClusterDeployment,
  enrollAgentNode,
  getAgentCommands,
  getAgentNodes,
  getClusterDeployments,
  getProjects,
  getTenants,
  revokeAgentNode,
  type AgentCommand,
  type AgentNode,
  type ClusterDeployment,
  type Project,
  type Tenant,
} from "../services/api";

const tenants = ref<Tenant[]>([]);
const projects = ref<Project[]>([]);
const nodes = ref<AgentNode[]>([]);
const commands = ref<AgentCommand[]>([]);
const deployments = ref<ClusterDeployment[]>([]);
const selectedTenantId = ref("");
const selectedProjectId = ref("");
const selectedNodeId = ref("");
const nodeName = ref("eph-node-01");
const nodeType = ref<AgentNode["node_type"]>("eph_node");
const clusterName = ref("");
const buildClusterName = ref("");
const buildKubernetesVersion = ref("");
const buildControlPlaneReplicas = ref(3);
const buildWorkerReplicas = ref(3);
const commandType = ref("node.health");
const commandPayload = ref("{}");
const enrollmentToken = ref("");
const enrollmentNodeId = ref("");
const error = ref("");
const info = ref("");
const loading = ref(false);
const submitting = ref(false);
let refreshTimer: number | undefined;

const selectedNode = computed(
  () => nodes.value.find((node) => node.id === selectedNodeId.value) ?? null,
);
const availableCommands = computed(() => {
  if (selectedNode.value?.node_type === "target_cluster") {
    return [
      "node.health",
      "cluster.health",
      "cluster.observe",
      "cluster.diagnose",
      "cluster.postflight",
      "k8s.resource.read",
    ];
  }
  return [
    "node.health",
    "host.preflight",
    "cluster.plan",
    "capi.cluster.observe",
    "metal3.host.inspect",
  ];
});

function defaultPayload(type: string): string {
  const values: Record<string, Record<string, unknown>> = {
    "node.health": {},
    "host.preflight": {},
    "cluster.health": {},
    "cluster.observe": {},
    "cluster.diagnose": { namespace: "default" },
    "cluster.postflight": {},
    "k8s.resource.read": { resource: "nodes" },
    "capi.cluster.observe": {},
    "metal3.host.inspect": {},
    "cluster.plan": {
      cluster_spec: {
        cluster_name: "edge-lab",
        kubernetes_version: "v1.32.2",
        control_plane_replicas: 3,
        worker_replicas: 4,
      },
    },
    "cluster.provision": {
      cluster_spec: {
        cluster_name: "edge-lab",
        kubernetes_version: "v1.32.2",
        control_plane_replicas: 3,
        worker_replicas: 4,
      },
    },
    "target-agent.bootstrap": { target_cluster_name: "edge-lab" },
  };
  return JSON.stringify(values[type] ?? {}, null, 2);
}

async function loadTenants(): Promise<void> {
  loading.value = true;
  try {
    const response = await getTenants();
    tenants.value = response.results;
    if (!selectedTenantId.value && tenants.value.length) {
      selectedTenantId.value = tenants.value[0].id;
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load tenants.";
  } finally {
    loading.value = false;
  }
}

async function loadProjects(tenantId: string): Promise<void> {
  projects.value = [];
  nodes.value = [];
  commands.value = [];
  selectedProjectId.value = "";
  selectedNodeId.value = "";
  if (!tenantId) return;
  try {
    const response = await getProjects(tenantId);
    projects.value = response.results;
    if (projects.value.length) selectedProjectId.value = projects.value[0].id;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load projects.";
  }
}

async function loadNodes(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) {
    nodes.value = [];
    commands.value = [];
    return;
  }
  try {
    const response = await getAgentNodes(selectedTenantId.value, selectedProjectId.value);
    nodes.value = response.results;
    if (!nodes.value.some((node) => node.id === selectedNodeId.value)) {
      selectedNodeId.value = nodes.value[0]?.id ?? "";
    }
    await loadDeployments();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load node agents.";
  }
}

async function loadDeployments(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) {
    deployments.value = [];
    return;
  }
  try {
    const response = await getClusterDeployments(
      selectedTenantId.value,
      selectedProjectId.value,
    );
    deployments.value = response.results;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load cluster plans.";
  }
}

async function loadCommands(): Promise<void> {
  if (!selectedNodeId.value) {
    commands.value = [];
    return;
  }
  try {
    const response = await getAgentCommands(selectedNodeId.value);
    commands.value = response.results;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load agent commands.";
  }
}

async function enrollNode(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  submitting.value = true;
  error.value = "";
  info.value = "";
  enrollmentToken.value = "";
  try {
    const enrolled = await enrollAgentNode(selectedTenantId.value, selectedProjectId.value, {
      name: nodeName.value.trim(),
      node_type: nodeType.value,
      cluster_name: clusterName.value.trim(),
    });
    enrollmentToken.value = enrolled.enrollment_token;
    enrollmentNodeId.value = enrolled.id;
    selectedNodeId.value = enrolled.id;
    info.value = "Node enrolled. Copy the token now; it will not be shown again.";
    await loadNodes();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to enroll node.";
  } finally {
    submitting.value = false;
  }
}

async function copyEnrollmentToken(): Promise<void> {
  if (!enrollmentToken.value) return;
  try {
    await navigator.clipboard.writeText(enrollmentToken.value);
    info.value = "Enrollment token copied.";
  } catch {
    error.value = "Clipboard access was denied. Select and copy the token manually.";
  }
}

async function generateClusterPlan(): Promise<void> {
  if (!selectedNode.value || selectedNode.value.node_type !== "eph_node") return;
  if (!buildClusterName.value.trim() || !buildKubernetesVersion.value.trim()) {
    error.value = "Cluster name and Kubernetes version are required.";
    return;
  }
  submitting.value = true;
  error.value = "";
  info.value = "";
  try {
    const deployment = await createClusterDeployment(
      selectedTenantId.value,
      selectedProjectId.value,
      selectedNode.value.id,
      {
        cluster_name: buildClusterName.value.trim(),
        kubernetes_version: buildKubernetesVersion.value.trim(),
        control_plane_replicas: buildControlPlaneReplicas.value,
        worker_replicas: buildWorkerReplicas.value,
      },
    );
    info.value =
      deployment.status === "plan_ready"
        ? "Cluster plan completed. Review the missing prerequisites before provisioning."
        : "Cluster delivery workflow created. EPH preflight and plan commands are queued.";
    await loadDeployments();
    await loadCommands();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create cluster plan.";
  } finally {
    submitting.value = false;
  }
}

async function dispatchCommand(): Promise<void> {
  if (!selectedNodeId.value) return;
  submitting.value = true;
  error.value = "";
  info.value = "";
  try {
    const payload: unknown = JSON.parse(commandPayload.value);
    if (payload === null || Array.isArray(payload) || typeof payload !== "object") {
      throw new Error("Command payload must be a JSON object.");
    }
    const created = await createAgentCommand(
      selectedNodeId.value,
      commandType.value,
      payload as Record<string, unknown>,
    );
    info.value =
      created.status === "waiting_approval"
        ? "Command queued for independent administrator approval."
        : "Command dispatched to the node queue.";
    await loadCommands();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to dispatch command.";
  } finally {
    submitting.value = false;
  }
}

async function cancelDeployment(deployment: ClusterDeployment): Promise<void> {
  if (!window.confirm("Cancel this cluster plan workflow?")) return;
  try {
    await cancelClusterDeployment(deployment.id);
    info.value = "Cluster plan workflow cancelled.";
    await loadDeployments();
    await loadCommands();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to cancel cluster plan.";
  }
}

function missingRequirements(deployment: ClusterDeployment): string[] {
  const value = deployment.plan_result?.missing_requirements;
  return Array.isArray(value) ? value.map(String) : [];
}

async function approveCommand(command: AgentCommand): Promise<void> {
  submitting.value = true;
  error.value = "";
  try {
    await approveAgentCommand(command.id, command.plan_hash);
    info.value = "Command approved. The node agent can now claim it.";
    await loadCommands();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Approval failed.";
  } finally {
    submitting.value = false;
  }
}

async function revokeNode(node: AgentNode): Promise<void> {
  if (!window.confirm("Revoke this node credential and cancel queued commands?")) return;
  try {
    await revokeAgentNode(node.id);
    info.value = "Node credential revoked.";
    await loadNodes();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to revoke node.";
  }
}

function statusType(status: string): "success" | "warning" | "danger" | "info" {
  if (status === "online" || status === "succeeded") return "success";
  if (status === "running" || status === "queued") return "warning";
  if (status === "failed" || status === "revoked") return "danger";
  return "info";
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "Never";
}

watch(selectedTenantId, (tenantId) => void loadProjects(tenantId));
watch(selectedProjectId, () => void loadNodes());
watch(selectedNodeId, () => {
  commandType.value = availableCommands.value[0] ?? "node.health";
  commandPayload.value = defaultPayload(commandType.value);
  void loadCommands();
});
watch(commandType, (value) => {
  commandPayload.value = defaultPayload(value);
});

onMounted(() => {
  void loadTenants();
  refreshTimer = window.setInterval(() => {
    void loadNodes();
    void loadCommands();
  }, 5000);
});
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
});
</script>

<template>
  <section class="infra-agents">
    <div class="infra-heading">
      <div>
        <p class="eyebrow">REMOTE INFRASTRUCTURE CONTROL</p>
        <h2>EPH &amp; Target Cluster Agents</h2>
        <p class="infra-description">
          One remote control plane, two independently enrolled execution agents. EPH provisions the
          target through CAPI / Metal3 / Ironic; the target agent joins after the Kubernetes API is ready.
        </p>
      </div>
      <el-tag type="success" effect="plain">OUTBOUND CONTROL CHANNEL</el-tag>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="info" :title="info" type="success" show-icon :closable="true" @close="info = ''" />

    <div class="infra-scope">
      <label>Organization
        <el-select v-model="selectedTenantId" placeholder="Select organization">
          <el-option v-for="tenant in tenants" :key="tenant.id" :label="tenant.name" :value="tenant.id" />
        </el-select>
      </label>
      <label>Project
        <el-select v-model="selectedProjectId" placeholder="Select project">
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
      </label>
    </div>

    <div class="infra-architecture">
      <article class="infra-role-card eph">
        <span class="role-index">01 · BOOTSTRAP PLANE</span>
        <h3>EPH Node Agent</h3>
        <p>Host preflight, management Kubernetes checks, CAPI / Metal3 discovery and cluster plan coordination.</p>
        <el-tag type="warning" effect="plain">Runs before target cluster exists</el-tag>
      </article>
      <div class="infra-link"><span>Remote command channel</span><b>⇄</b></div>
      <article class="infra-role-card target">
        <span class="role-index">02 · TARGET PLANE</span>
        <h3>Target Cluster Agent</h3>
        <p>Bootstrapped after API readiness; performs Kubernetes health, node readiness, events and scoped read-only inspection.</p>
        <el-tag type="success" effect="plain">Joins after provisioning</el-tag>
      </article>
    </div>

    <div class="infra-columns">
      <section class="infra-panel">
        <div class="infra-panel-heading">
          <div><p class="eyebrow">NODE REGISTRY</p><h3>Enroll an Agent</h3></div>
          <el-tag effect="plain">{{ nodes.length }} registered</el-tag>
        </div>
        <div class="infra-form">
          <label>Agent type
            <el-select v-model="nodeType">
              <el-option label="EPH Node Agent" value="eph_node" />
              <el-option label="Target Cluster Agent" value="target_cluster" />
            </el-select>
          </label>
          <label>Node name
            <el-input v-model="nodeName" placeholder="eph-node-01" />
          </label>
          <label>Target cluster name (optional)
            <el-input v-model="clusterName" placeholder="edge-lab" />
          </label>
          <el-button type="primary" :loading="submitting" :disabled="!selectedProjectId || !nodeName.trim()" @click="enrollNode">
            Enroll node and issue one-time token
          </el-button>
        </div>

        <div v-if="enrollmentToken" class="enrollment-secret">
          <strong>One-time enrollment token</strong>
          <p>Save this value in a secret manager on the agent host. The API stores only its hash.</p>
          <el-input :model-value="enrollmentToken" readonly />
          <el-button size="small" @click="copyEnrollmentToken">Copy token</el-button>
          <code>AGENT_NODE_ID={{ enrollmentNodeId }}</code>
        </div>

        <div class="infra-node-list">
          <button
            v-for="node in nodes"
            :key="node.id"
            class="infra-node"
            :class="{ selected: selectedNodeId === node.id }"
            @click="selectedNodeId = node.id"
          >
            <span class="node-symbol">{{ node.node_type === "eph_node" ? "E" : "T" }}</span>
            <span class="node-main">
              <strong>{{ node.name }}</strong>
              <small>{{ node.node_type }} · {{ node.cluster_name || "cluster not assigned" }}</small>
              <small>Last heartbeat: {{ formatTime(node.last_seen_at) }}</small>
            </span>
            <el-tag :type="statusType(node.status)" size="small">{{ node.status }}</el-tag>
          </button>
          <el-empty v-if="!loading && !nodes.length" description="No agents enrolled in this project" :image-size="72" />
        </div>
      </section>

      <section class="infra-panel">
        <div class="infra-panel-heading">
          <div><p class="eyebrow">COMMAND DISPATCH</p><h3>Remote Agent Console</h3></div>
          <el-tag v-if="selectedNode" :type="statusType(selectedNode.status)">{{ selectedNode.status }}</el-tag>
        </div>
        <el-empty v-if="!selectedNode" description="Enroll or select an agent to dispatch a command" :image-size="82" />
        <template v-else>
          <div class="selected-node-summary">
            <strong>{{ selectedNode.name }}</strong>
            <span>{{ selectedNode.node_type }} · {{ selectedNode.agent_version || "version not reported" }}</span>
          </div>
          <div v-if="selectedNode.node_type === 'eph_node'" class="infra-form cluster-build-form">
            <div>
              <strong>Metal3 / CAPI cluster plan</strong>
              <p>Build a typed Kubernetes delivery plan on the EPH agent. This stage is dry-run only.</p>
            </div>
            <label>Target cluster name
              <el-input v-model="buildClusterName" placeholder="edge-lab" />
            </label>
            <label>Kubernetes version
              <el-input v-model="buildKubernetesVersion" placeholder="v1.x.y" />
            </label>
            <div class="replica-fields">
              <label>Control plane replicas
                <el-input-number v-model="buildControlPlaneReplicas" :min="1" :max="9" />
              </label>
              <label>Worker replicas
                <el-input-number v-model="buildWorkerReplicas" :min="0" :max="1000" />
              </label>
            </div>
            <el-button
              type="primary"
              plain
              :loading="submitting"
              :disabled="!buildClusterName.trim() || !buildKubernetesVersion.trim()"
              @click="generateClusterPlan"
            >
              Generate cluster plan
            </el-button>
          </div>
          <div class="infra-form">
            <label>Typed command
              <el-select v-model="commandType">
                <el-option v-for="item in availableCommands" :key="item" :label="item" :value="item" />
              </el-select>
            </label>
            <label>JSON payload
              <el-input v-model="commandPayload" type="textarea" :rows="8" spellcheck="false" />
            </label>
            <el-button type="primary" :loading="submitting" :disabled="selectedNode.status === 'revoked'" @click="dispatchCommand">
              Dispatch command
            </el-button>
          </div>
          <div class="deployment-list">
            <div class="infra-panel-heading">
              <h4>Cluster delivery plans</h4>
              <span>{{ deployments.length }} recent</span>
            </div>
            <article v-for="deployment in deployments" :key="deployment.id" class="deployment-card">
              <div class="command-topline">
                <strong>{{ deployment.cluster_name }} · {{ deployment.kubernetes_version }}</strong>
                <el-tag :type="statusType(deployment.status)" size="small">{{ deployment.status }}</el-tag>
              </div>
              <small>{{ formatTime(deployment.created_at) }} · EPH {{ deployment.eph_node_id.slice(0, 8) }}</small>
              <div v-if="deployment.plan_result" class="plan-result">
                <span>Plan hash: {{ deployment.plan_hash || "not generated" }}</span>
                <el-tag
                  :type="deployment.plan_result.adapter_enabled === true ? 'success' : 'warning'"
                  size="small"
                >
                  {{ deployment.plan_result.adapter_enabled === true ? "Adapter enabled" : "Apply adapter disabled" }}
                </el-tag>
                <ul v-if="missingRequirements(deployment).length">
                  <li v-for="item in missingRequirements(deployment)" :key="item">{{ item }}</li>
                </ul>
                <p v-else>Input prerequisites are complete. Compatibility and real provisioning are still separate gates.</p>
              </div>
              <el-button
                v-if="['preflight_queued', 'planning', 'plan_ready'].includes(deployment.status)"
                size="small"
                text
                type="danger"
                @click="cancelDeployment(deployment)"
              >
                Cancel plan
              </el-button>
            </article>
            <el-empty v-if="!deployments.length" description="No cluster plan workflows created" :image-size="56" />
          </div>
          <div class="command-list">
            <div class="infra-panel-heading"><h4>Command history</h4><span>{{ commands.length }} recent</span></div>
            <article v-for="command in commands" :key="command.id" class="command-card">
              <div class="command-topline">
                <strong>{{ command.command_type }}</strong>
                <el-tag :type="statusType(command.status)" size="small">{{ command.status }}</el-tag>
              </div>
              <small>{{ formatTime(command.created_at) }} · attempts {{ command.attempts }}</small>
              <p v-if="command.error" class="command-error">{{ command.error.code || "Execution failed" }}</p>
              <pre v-if="command.result">{{ JSON.stringify(command.result, null, 2) }}</pre>
              <div v-if="command.status === 'waiting_approval'" class="approval-row">
                <code>{{ command.plan_hash }}</code>
                <el-button size="small" type="warning" :loading="submitting" @click="approveCommand(command)">
                  Approve plan hash
                </el-button>
              </div>
            </article>
            <el-empty v-if="!commands.length" description="No commands dispatched yet" :image-size="60" />
          </div>
        </template>
      </section>
    </div>

    <div class="infra-security-note">
      <strong>Execution safety boundary</strong>
      <p>
        This release supports enrollment, heartbeat, durable command polling, lease-protected results,
        approval-hash verification, host preflight, cluster-plan dry run and read-only Kubernetes API
        inspection. Cluster creation, raw manifest application, arbitrary shell, BMC power operations
        and target-agent bootstrap are not enabled until the CAPI / Metal3 / Ironic compatibility matrix
        and execution adapters are verified.
      </p>
    </div>
  </section>
</template>
