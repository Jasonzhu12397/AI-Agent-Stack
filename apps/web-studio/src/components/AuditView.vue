<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  getAgentCommands,
  getAgentNodes,
  getAuditEvents,
  verifyAuditChain,
  type AuditEvent,
  type AuditChainVerification,
  getClusterDeployments,
  getTasks,
  type AgentCommand,
  type AgentNode,
  type ClusterDeployment,
  type Task,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();
const nodes = ref<AgentNode[]>([]);
const commands = ref<AgentCommand[]>([]);
const deployments = ref<ClusterDeployment[]>([]);
const tasks = ref<Task[]>([]);
const auditEvents = ref<AuditEvent[]>([]);
const chainVerification = ref<AuditChainVerification | null>(null);
const auditError = ref("");
const loading = ref(false);
const error = ref("");

type Activity = { id: string; at: string; source: string; action: string; status: string; detail: string };
const activities = computed<Activity[]>(() => [
  ...tasks.value.map((task) => ({
    id: "task-" + task.id, at: task.finished_at ?? task.created_at, source: "Task queue",
    action: task.task_type, status: task.status, detail: task.id,
  })),
  ...commands.value.map((command) => ({
    id: "command-" + command.id, at: command.finished_at ?? command.started_at ?? command.created_at,
    source: nodes.value.find((node) => node.id === command.node_id)?.name ?? "Agent node",
    action: command.command_type, status: command.status, detail: command.id,
  })),
  ...deployments.value.map((deployment) => ({
    id: "deployment-" + deployment.id, at: deployment.updated_at, source: "Cluster deployment",
    action: deployment.cluster_name, status: deployment.status, detail: deployment.id,
  })),
].sort((left, right) => right.at.localeCompare(left.at)).slice(0, 100));

async function refresh(): Promise<void> {
  if (!props.tenantId || !props.projectId) {
    nodes.value = []; commands.value = []; deployments.value = []; tasks.value = []; auditEvents.value = []; chainVerification.value = null;
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const [taskPage, nodePage, deploymentPage] = await Promise.all([
      getTasks(props.tenantId, props.projectId),
      getAgentNodes(props.tenantId, props.projectId),
      getClusterDeployments(props.tenantId, props.projectId),
    ]);
    tasks.value = taskPage.results;
    nodes.value = nodePage.results;
    deployments.value = deploymentPage.results;
    const commandPages = await Promise.all(nodes.value.map((node) => getAgentCommands(node.id)));
    commands.value = commandPages.flatMap((page) => page.results);
    auditError.value = "";
    try {
      const [auditPage, verification] = await Promise.all([
        getAuditEvents(props.tenantId, props.projectId),
        verifyAuditChain(props.tenantId),
      ]);
      auditEvents.value = auditPage.results;
      chainVerification.value = verification;
    } catch (cause) {
      auditEvents.value = [];
      chainVerification.value = null;
      auditError.value = cause instanceof Error ? cause.message : "Audit access is unavailable.";
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load operational history.";
  } finally {
    loading.value = false;
  }
}

function tagType(value: string): "success" | "warning" | "danger" | "info" {
  if (["succeeded", "online", "healthy"].includes(value)) return "success";
  if (["queued", "running", "waiting_approval", "preflight_queued", "provisioning"].includes(value)) return "warning";
  if (["failed", "revoked", "cancelled"].includes(value)) return "danger";
  return "info";
}

watch(() => [props.tenantId, props.projectId], () => void refresh());
onMounted(() => void refresh());
</script>

<template>
  <section class="operations-view">
    <header class="operations-heading">
      <div><p class="eyebrow">EXECUTION TRACE</p><h3>Audit & Activity</h3>
        <p>Timeline assembled from persisted tasks, agent commands and cluster deployment records.</p>
      </div>
      <el-button plain :loading="loading" @click="refresh">Refresh</el-button>
    </header>
    <el-alert v-if="chainVerification?.valid" type="success" :closable="false" show-icon :title="'Audit hash chain verified: ' + chainVerification.verified_count + ' events.'">
    </el-alert>
    <el-alert v-else-if="chainVerification && !chainVerification.valid" type="error" :closable="false" show-icon :title="'Audit chain integrity failure at event ' + chainVerification.invalid_event_id" />
    <el-alert v-if="auditError" :title="auditError" type="warning" show-icon :closable="true" @close="auditError = ''" />
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <div class="operations-panel">
      <div class="operations-panel-heading"><h4>Security audit events</h4><span>APPEND-ONLY · HASH-CHAINED</span></div>
      <el-table :data="auditEvents" size="small" class="task-table">
        <el-table-column label="Time" min-width="165">
          <template #default="scope">{{ new Date(scope.row.created_at).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column prop="actor_username" label="Actor" min-width="120" />
        <el-table-column prop="event_type" label="Event" min-width="210" />
        <el-table-column prop="resource_type" label="Resource" min-width="140" />
        <el-table-column label="Outcome" min-width="100">
          <template #default="scope"><el-tag :type="scope.row.outcome === 'success' ? 'success' : scope.row.outcome === 'denied' ? 'warning' : 'danger'" size="small">{{ scope.row.outcome }}</el-tag></template>
        </el-table-column>
        <el-table-column label="Event hash" min-width="150">
          <template #default="scope"><code>{{ scope.row.event_hash.slice(0, 16) }}</code></template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !auditEvents.length && !auditError" description="No security audit events recorded yet" :image-size="56" />
      <p class="operations-note">The per-tenant hash chain detects event alteration or removal within the retained chain. Current event coverage includes Agent node enrollment/revocation, command queue/approval, connector approval/installation/status changes and cluster deployment create/cancel/handoff. Backup, retention and external WORM export are still required for production-grade immutability.</p>
    </div>
    <div class="operations-panel">
      <div class="operations-panel-heading"><h4>Recent activity</h4><span>LAST 100 RECORDS</span></div>
      <el-table :data="activities" size="small" class="task-table">
        <el-table-column label="Time" min-width="165">
          <template #default="scope">{{ new Date(scope.row.at).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column prop="source" label="Source" min-width="140" />
        <el-table-column prop="action" label="Action" min-width="170" />
        <el-table-column label="Status" min-width="125">
          <template #default="scope"><el-tag :type="tagType(scope.row.status)" size="small">{{ scope.row.status }}</el-tag></template>
        </el-table-column>
        <el-table-column label="Record ID" min-width="140">
          <template #default="scope"><code>{{ scope.row.detail.slice(0, 12) }}</code></template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !activities.length" description="No persisted execution records in this project" :image-size="60" />
    </div>
  </section>
</template>
