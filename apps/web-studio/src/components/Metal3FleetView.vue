<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  getAgentCommands,
  getAgentNodes,
  getClusterDeployments,
  getProjects,
  getTenants,
  type AgentCommand,
  type AgentNode,
  type ClusterDeployment,
  type Project,
  type Tenant,
} from "../services/api";
import InfrastructureAgentsView from "./InfrastructureAgentsView.vue";

type FleetTab = "dashboard" | "fleet" | "eph" | "clusters" | "terminal" | "operations";

const tabs: { id: FleetTab; label: string }[] = [
  { id: "dashboard", label: "Overview" },
  { id: "fleet", label: "Fleet Inventory" },
  { id: "eph", label: "EPH Bootstrap" },
  { id: "clusters", label: "Target Clusters" },
  { id: "terminal", label: "Execution Terminal" },
  { id: "operations", label: "Operations Console" },
];

const activeTab = ref<FleetTab>("dashboard");
const tenants = ref<Tenant[]>([]);
const projects = ref<Project[]>([]);
const nodes = ref<AgentNode[]>([]);
const deployments = ref<ClusterDeployment[]>([]);
const commands = ref<AgentCommand[]>([]);
const selectedTenantId = ref("");
const selectedProjectId = ref("");
const loading = ref(false);
const refreshing = ref(false);
const error = ref("");
let refreshTimer: number | undefined;

const ephNodes = computed(() => nodes.value.filter((node) => node.node_type === "eph_node"));
const targetAgents = computed(() => nodes.value.filter((node) => node.node_type === "target_cluster"));
const onlineNodes = computed(() => nodes.value.filter((node) => node.status === "online"));
const failedDeployments = computed(() => deployments.value.filter((item) => item.status === "failed"));
const activeDeployments = computed(() =>
  deployments.value.filter((item) => !["succeeded", "failed", "cancelled"].includes(item.status)),
);
const recentCommands = computed(() =>
  [...commands.value].sort((left, right) => right.created_at.localeCompare(left.created_at)).slice(0, 80),
);
const latestActivity = computed(() => {
  const commandTimes = commands.value.map((item) => item.finished_at ?? item.started_at ?? item.created_at);
  const deploymentTimes = deployments.value.map((item) => item.updated_at);
  return [...commandTimes, ...deploymentTimes].sort().at(-1) ?? null;
});

async function loadTenants(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    const response = await getTenants();
    tenants.value = response.results;
    if (!tenants.value.some((tenant) => tenant.id === selectedTenantId.value)) {
      selectedTenantId.value = tenants.value[0]?.id ?? "";
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load tenant scope.";
  } finally {
    loading.value = false;
  }
}

async function loadProjects(tenantId: string): Promise<void> {
  projects.value = [];
  nodes.value = [];
  deployments.value = [];
  commands.value = [];
  selectedProjectId.value = "";
  if (!tenantId) return;
  try {
    const response = await getProjects(tenantId);
    projects.value = response.results;
    selectedProjectId.value = projects.value[0]?.id ?? "";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load project scope.";
  }
}

async function refreshData(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) {
    nodes.value = [];
    deployments.value = [];
    commands.value = [];
    return;
  }
  refreshing.value = true;
  error.value = "";
  try {
    const [nodePage, deploymentPage] = await Promise.all([
      getAgentNodes(selectedTenantId.value, selectedProjectId.value),
      getClusterDeployments(selectedTenantId.value, selectedProjectId.value),
    ]);
    nodes.value = nodePage.results;
    deployments.value = deploymentPage.results;
    const commandPages = await Promise.all(
      nodes.value.map((node) => getAgentCommands(node.id)),
    );
    commands.value = commandPages.flatMap((page) => page.results);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to refresh Metal3 fleet data.";
  } finally {
    refreshing.value = false;
  }
}

function statusType(status: string): "success" | "warning" | "danger" | "info" {
  if (["online", "succeeded", "healthy"].includes(status)) return "success";
  if (["running", "queued", "waiting_approval", "provisioning", "preflight_queued"].includes(status)) return "warning";
  if (["failed", "revoked", "offline"].includes(status)) return "danger";
  return "info";
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function nodeLabel(nodeId: string): string {
  return nodes.value.find((node) => node.id === nodeId)?.name ?? nodeId.slice(0, 8);
}

function commandDetail(command: AgentCommand): string {
  if (command.error) return JSON.stringify(command.error);
  if (command.result) return JSON.stringify(command.result);
  return JSON.stringify(command.payload);
}

watch(selectedTenantId, (tenantId) => void loadProjects(tenantId));
watch(selectedProjectId, () => void refreshData());

onMounted(async () => {
  await loadTenants();
  refreshTimer = window.setInterval(() => void refreshData(), 15000);
});
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
});
</script>

<template>
  <section class="metal3-fleet">
    <header class="fleet-heading">
      <div>
        <p class="eyebrow">BARE-METAL CONTROL PLANE</p>
        <h2>Metal3 Fleet</h2>
        <p>EPH bootstrap agents, Target Cluster agents, deployment workflows and persisted command traces.</p>
      </div>
      <div class="fleet-scope">
        <el-select v-model="selectedTenantId" placeholder="Tenant" size="small" style="width: 180px">
          <el-option v-for="tenant in tenants" :key="tenant.id" :label="tenant.name" :value="tenant.id" />
        </el-select>
        <el-select v-model="selectedProjectId" placeholder="Project" size="small" style="width: 180px">
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
        <el-button plain size="small" :loading="refreshing || loading" @click="refreshData">Refresh</el-button>
      </div>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />

    <nav class="fleet-tabs">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >{{ tab.label }}</button>
      <span class="fleet-live"><i></i> POLL 15s</span>
    </nav>

    <template v-if="activeTab === 'dashboard'">
      <div class="fleet-metrics">
        <article><span>Online Agents</span><strong>{{ onlineNodes.length }} / {{ nodes.length }}</strong><small>Current project scope</small></article>
        <article><span>EPH Bootstrap Nodes</span><strong>{{ ephNodes.length }}</strong><small>Temporary management plane</small></article>
        <article><span>Target Cluster Agents</span><strong>{{ targetAgents.length }}</strong><small>Target-side supervisors</small></article>
        <article><span>Active Deployments</span><strong>{{ activeDeployments.length }}</strong><small>{{ failedDeployments.length }} failed</small></article>
      </div>
      <div class="fleet-two-column">
        <section class="fleet-panel">
          <div class="fleet-panel-heading"><h3>Agent topology</h3><span>{{ nodes.length }} NODES</span></div>
          <div v-for="node in nodes" :key="node.id" class="fleet-node-row">
            <div class="fleet-node-icon"><el-icon><Monitor /></el-icon></div>
            <div class="fleet-node-main">
              <strong>{{ node.name }}</strong>
              <small>{{ node.node_type === "eph_node" ? "EPH bootstrap" : "Target cluster" }} · {{ node.cluster_name || "cluster unassigned" }}</small>
            </div>
            <el-tag :type="statusType(node.status)" size="small">{{ node.status }}</el-tag>
          </div>
          <el-empty v-if="!loading && !nodes.length" description="No Agent nodes enrolled in this project" :image-size="56" />
        </section>
        <section class="fleet-panel">
          <div class="fleet-panel-heading"><h3>Deployment pipeline</h3><span>{{ deployments.length }} CLUSTERS</span></div>
          <div v-for="deployment in deployments.slice(0, 6)" :key="deployment.id" class="fleet-deployment-row">
            <div><strong>{{ deployment.cluster_name }}</strong><small>{{ deployment.kubernetes_version }} · {{ formatTime(deployment.updated_at) }}</small></div>
            <el-tag :type="statusType(deployment.status)" size="small">{{ deployment.status }}</el-tag>
          </div>
          <el-empty v-if="!loading && !deployments.length" description="No cluster delivery workflows yet" :image-size="56" />
        </section>
      </div>
      <p class="fleet-footnote">Last recorded activity: {{ formatTime(latestActivity) }}. Data is sourced from persisted Agent node, command and ClusterDeployment APIs; no simulated hardware telemetry is displayed.</p>
    </template>

    <template v-else-if="activeTab === 'fleet'">
      <section class="fleet-panel">
        <div class="fleet-panel-heading"><h3>Bare-metal Agent Inventory</h3><span>{{ nodes.length }} REGISTERED</span></div>
        <el-table :data="nodes" size="small" class="task-table">
          <el-table-column prop="name" label="Agent / Node" min-width="160" />
          <el-table-column label="Role" min-width="140">
            <template #default="scope">{{ scope.row.node_type === "eph_node" ? "EPH Bootstrap" : "Target Cluster" }}</template>
          </el-table-column>
          <el-table-column prop="cluster_name" label="Cluster" min-width="150" />
          <el-table-column label="Status" min-width="110">
            <template #default="scope"><el-tag :type="statusType(scope.row.status)" size="small">{{ scope.row.status }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="agent_version" label="Agent Version" min-width="130" />
          <el-table-column label="Last Heartbeat" min-width="170">
            <template #default="scope">{{ formatTime(scope.row.last_seen_at) }}</template>
          </el-table-column>
          <el-table-column prop="last_seen_ip" label="Source IP" min-width="140" />
          <el-table-column label="Capabilities" min-width="180">
            <template #default="scope">{{ scope.row.capabilities.join(", ") || "—" }}</template>
          </el-table-column>
        </el-table>
      </section>
    </template>

    <template v-else-if="activeTab === 'eph'">
      <section class="fleet-panel">
        <div class="fleet-panel-heading"><h3>EPH Temporary Bootstrap Nodes</h3><span>{{ ephNodes.length }} NODES</span></div>
        <div v-for="node in ephNodes" :key="node.id" class="eph-node-card">
          <div class="eph-node-header">
            <div><p class="eyebrow">TEMPORARY MANAGEMENT PLANE</p><h3>{{ node.name }}</h3></div>
            <el-tag :type="statusType(node.status)" size="small">{{ node.status }}</el-tag>
          </div>
          <div class="eph-node-grid">
            <div><span>Cluster binding</span><strong>{{ node.cluster_name || "Not assigned" }}</strong></div>
            <div><span>Agent version</span><strong>{{ node.agent_version }}</strong></div>
            <div><span>Last heartbeat</span><strong>{{ formatTime(node.last_seen_at) }}</strong></div>
            <div><span>Last source IP</span><strong>{{ node.last_seen_ip || "—" }}</strong></div>
          </div>
          <div class="fleet-capabilities"><span v-for="capability in node.capabilities" :key="capability">{{ capability }}</span></div>
          <p class="fleet-footnote">EPH lifecycle: RAM-booted single-node Kubernetes, EPH Agent takeover, CAPI management pivot, then hardware reclaim as a Target Worker. The steps are shown as architecture requirements; this page reports only the Agent state currently confirmed by the backend.</p>
        </div>
        <el-empty v-if="!loading && !ephNodes.length" description="No EPH bootstrap Agent enrolled" :image-size="56" />
      </section>
    </template>

    <template v-else-if="activeTab === 'clusters'">
      <section class="fleet-panel">
        <div class="fleet-panel-heading"><h3>Target Cluster Delivery</h3><span>{{ deployments.length }} WORKFLOWS</span></div>
        <el-table :data="deployments" size="small" class="task-table">
          <el-table-column prop="cluster_name" label="Cluster" min-width="150" />
          <el-table-column prop="kubernetes_version" label="Kubernetes" min-width="120" />
          <el-table-column prop="status" label="Workflow Status" min-width="160">
            <template #default="scope"><el-tag :type="statusType(scope.row.status)" size="small">{{ scope.row.status }}</el-tag></template>
          </el-table-column>
          <el-table-column label="EPH Agent" min-width="140">
            <template #default="scope">{{ nodeLabel(scope.row.eph_node_id) }}</template>
          </el-table-column>
          <el-table-column label="Target Agent" min-width="140">
            <template #default="scope">{{ scope.row.target_node_id ? nodeLabel(scope.row.target_node_id) : "Pending handoff" }}</template>
          </el-table-column>
          <el-table-column label="Updated" min-width="170">
            <template #default="scope">{{ formatTime(scope.row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="Plan Hash" min-width="130">
            <template #default="scope"><code>{{ scope.row.plan_hash.slice(0, 14) }}</code></template>
          </el-table-column>
        </el-table>
      </section>
    </template>

    <template v-else-if="activeTab === 'terminal'">
      <section class="fleet-terminal">
        <div class="fleet-terminal-bar"><span>AGENT COMMAND STREAM</span><span>{{ recentCommands.length }} RECORDS · PERSISTED POLL</span></div>
        <div class="fleet-terminal-body">
          <div v-for="command in recentCommands" :key="command.id" class="fleet-terminal-line">
            <span class="terminal-time">{{ formatTime(command.finished_at ?? command.started_at ?? command.created_at) }}</span>
            <span class="terminal-node">{{ nodeLabel(command.node_id) }}</span>
            <span class="terminal-command">{{ command.command_type }}</span>
            <el-tag :type="statusType(command.status)" size="small">{{ command.status }}</el-tag>
            <pre>{{ commandDetail(command) }}</pre>
          </div>
          <el-empty v-if="!recentCommands.length" description="No persisted Agent commands yet" :image-size="56" />
        </div>
      </section>
    </template>

    <template v-else-if="activeTab === 'operations'">
      <InfrastructureAgentsView />
    </template>
  </section>
</template>
