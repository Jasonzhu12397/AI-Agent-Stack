<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  approveAgentCommand,
  approvePlugin,
  getAgentCommands,
  getAgentNodes,
  getPluginCatalog,
  type AgentCommand,
  type AgentNode,
  type PluginDefinition,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();
const nodes = ref<AgentNode[]>([]);
const commands = ref<AgentCommand[]>([]);
const plugins = ref<PluginDefinition[]>([]);
const loading = ref(false);
const busyId = ref("");
const error = ref("");
const info = ref("");

const pendingCommands = computed(() => commands.value.filter((item) => item.status === "waiting_approval"));
const draftPlugins = computed(() => plugins.value.filter((item) => item.review_status === "draft"));
const approvedPlugins = computed(() => plugins.value.filter((item) => item.review_status === "approved"));

async function refresh(): Promise<void> {
  if (!props.tenantId || !props.projectId) {
    nodes.value = [];
    commands.value = [];
    plugins.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const [nodePage, catalog] = await Promise.all([
      getAgentNodes(props.tenantId, props.projectId),
      getPluginCatalog(props.tenantId),
    ]);
    nodes.value = nodePage.results;
    plugins.value = catalog.results;
    const commandPages = await Promise.all(
      nodes.value.map((node) => getAgentCommands(node.id)),
    );
    commands.value = commandPages
      .flatMap((page) => page.results)
      .sort((left, right) => right.created_at.localeCompare(left.created_at));
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load governance data.";
  } finally {
    loading.value = false;
  }
}

async function approveCommand(command: AgentCommand): Promise<void> {
  if (!command.plan_hash) {
    error.value = "Approval refused: command has no plan hash.";
    return;
  }
  busyId.value = command.id;
  error.value = "";
  info.value = "";
  try {
    await approveAgentCommand(command.id, command.plan_hash);
    info.value = "Command approval recorded against the current plan hash.";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Command approval failed.";
  } finally {
    busyId.value = "";
  }
}

async function approveConnector(plugin: PluginDefinition): Promise<void> {
  busyId.value = plugin.id;
  error.value = "";
  info.value = "";
  try {
    await approvePlugin(plugin.id);
    info.value = plugin.name + " approved by the platform API.";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Connector review failed.";
  } finally {
    busyId.value = "";
  }
}

watch(() => [props.tenantId, props.projectId], () => void refresh());
onMounted(() => void refresh());
</script>

<template>
  <section class="operations-view">
    <header class="operations-heading">
      <div><p class="eyebrow">POLICY & APPROVALS</p><h3>Governance Center</h3>
        <p>Review actions against server-side authorization and plan-hash validation.</p>
      </div>
      <el-button plain :loading="loading" @click="refresh">Refresh</el-button>
    </header>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="info" :title="info" type="success" show-icon :closable="true" @close="info = ''" />
    <div class="operations-metrics">
      <article><span>Pending command approvals</span><strong>{{ pendingCommands.length }}</strong></article>
      <article><span>Connectors awaiting review</span><strong>{{ draftPlugins.length }}</strong></article>
      <article><span>Approved connectors</span><strong>{{ approvedPlugins.length }}</strong></article>
    </div>
    <section class="operations-panel">
      <div class="operations-panel-heading"><h4>Infrastructure command approvals</h4><span>PLAN-HASH BOUND</span></div>
      <el-table :data="pendingCommands" size="small" class="task-table">
        <el-table-column prop="command_type" label="Command" min-width="170" />
        <el-table-column label="Node" min-width="130">
          <template #default="scope">{{ nodes.find((node) => node.id === scope.row.node_id)?.name ?? scope.row.node_id.slice(0, 8) }}</template>
        </el-table-column>
        <el-table-column label="Plan hash" min-width="150">
          <template #default="scope"><code>{{ scope.row.plan_hash ? scope.row.plan_hash.slice(0, 16) : "missing" }}</code></template>
        </el-table-column>
        <el-table-column label="Created" min-width="150">
          <template #default="scope">{{ new Date(scope.row.created_at).toLocaleString() }}</template>
        </el-table-column>
        <el-table-column label="Action" width="120" fixed="right">
          <template #default="scope">
            <el-button type="primary" size="small" :loading="busyId === scope.row.id" :disabled="!scope.row.plan_hash" @click="approveCommand(scope.row)">Approve</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !pendingCommands.length" description="No infrastructure commands are waiting for approval" :image-size="60" />
    </section>
    <section class="operations-panel">
      <div class="operations-panel-heading"><h4>Connector review queue</h4><span>REGISTRY REVIEW</span></div>
      <div v-for="plugin in draftPlugins" :key="plugin.id" class="operations-row">
        <div><strong>{{ plugin.name }}</strong><small>{{ plugin.plugin_key }} · {{ plugin.version }} · {{ plugin.connector_type }}</small></div>
        <el-button size="small" type="primary" :loading="busyId === plugin.id" @click="approveConnector(plugin)">Approve connector</el-button>
      </div>
      <el-empty v-if="!loading && !draftPlugins.length" description="No connectors are awaiting review" :image-size="56" />
    </section>
    <p class="operations-note">Approval controls call the protected backend endpoints. They do not execute a deployment or bypass the separate command execution gates.</p>
  </section>
</template>
