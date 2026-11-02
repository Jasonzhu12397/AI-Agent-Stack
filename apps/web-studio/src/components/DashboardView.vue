<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  getAgentNodes,
  getTasks,
  type AgentNode,
  type Task,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();
const tasks = ref<Task[]>([]);
const nodes = ref<AgentNode[]>([]);
const error = ref("");
const loading = ref(false);

const activeTasks = computed(
  () => tasks.value.filter((task) => ["queued", "running", "cancel_requested"].includes(task.status)).length,
);
const successfulTasks = computed(
  () => tasks.value.filter((task) => task.status === "succeeded").length,
);
const onlineNodes = computed(
  () => nodes.value.filter((node) => node.status === "online").length,
);
const latestTasks = computed(() => tasks.value.slice(0, 5));

async function refresh(): Promise<void> {
  if (!props.tenantId || !props.projectId) {
    tasks.value = [];
    nodes.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const [taskPage, nodePage] = await Promise.all([
      getTasks(props.tenantId, props.projectId),
      getAgentNodes(props.tenantId, props.projectId),
    ]);
    tasks.value = taskPage.results;
    nodes.value = nodePage.results;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load overview metrics.";
  } finally {
    loading.value = false;
  }
}

function statusType(status: string): "success" | "warning" | "danger" | "info" {
  if (status === "succeeded" || status === "online") return "success";
  if (status === "running" || status === "queued") return "warning";
  if (status === "failed" || status === "revoked") return "danger";
  return "info";
}

watch(() => [props.tenantId, props.projectId], () => void refresh());
onMounted(() => void refresh());
</script>

<template>
  <section class="dashboard-view">
    <div class="dashboard-banner">
      <div>
        <span class="dashboard-state"><i></i> CONTROL PLANE CONNECTED</span>
        <h3>Operational overview</h3>
        <p>Live counts are scoped to the selected project. No demo metrics are substituted for missing backend data.</p>
      </div>
      <el-button plain :loading="loading" @click="refresh">Refresh data</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="true" show-icon />
    <div class="dashboard-metrics">
      <article>
        <span>Recent tasks</span>
        <strong>{{ tasks.length }}</strong>
        <small>Latest 50 project tasks</small>
      </article>
      <article>
        <span>Active queue</span>
        <strong>{{ activeTasks }}</strong>
        <small>Queued or running</small>
      </article>
      <article>
        <span>Successful tasks</span>
        <strong>{{ successfulTasks }}</strong>
        <small>Within current task page</small>
      </article>
      <article>
        <span>Online agents</span>
        <strong>{{ onlineNodes }} / {{ nodes.length }}</strong>
        <small>EPH and target cluster agents</small>
      </article>
    </div>
    <div class="dashboard-panels">
      <section class="dashboard-panel">
        <div class="dashboard-panel-heading"><h4>Recent execution</h4><span>POSTGRESQL TASK STATE</span></div>
        <div v-for="task in latestTasks" :key="task.id" class="dashboard-task">
          <div><strong>{{ task.task_type }}</strong><small>{{ task.id.slice(0, 12) }}</small></div>
          <el-tag :type="statusType(task.status)" size="small">{{ task.status }}</el-tag>
        </div>
        <el-empty v-if="!loading && !latestTasks.length" description="No task runs in this project" :image-size="56" />
      </section>
      <section class="dashboard-panel">
        <div class="dashboard-panel-heading"><h4>Infrastructure agents</h4><span>REMOTE EXECUTION NODES</span></div>
        <div v-for="node in nodes.slice(0, 5)" :key="node.id" class="dashboard-task">
          <div>
            <strong>{{ node.name }}</strong>
            <small>{{ node.node_type }} · {{ node.cluster_name || "unassigned cluster" }}</small>
          </div>
          <el-tag :type="statusType(node.status)" size="small">{{ node.status }}</el-tag>
        </div>
        <el-empty v-if="!loading && !nodes.length" description="Enroll an EPH or target agent to begin" :image-size="56" />
      </section>
    </div>
  </section>
</template>
