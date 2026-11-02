<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  createAgent,
  createAgentRun,
  getAgentRuns,
  getAgents,
  getKnowledgeBases,
  getPluginCatalog,
  getPluginInstallations,
  getModelConfigs,
  getModelProviders,
  publishAgent,
  type AgentDraft,
  type AgentRun,
  type KnowledgeBase,
  type ModelConfig,
  type PluginTool,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();
const agents = ref<AgentDraft[]>([]);
const models = ref<ModelConfig[]>([]);
const runs = ref<AgentRun[]>([]);
type SelectableTool = PluginTool & { plugin_name: string; plugin_key: string };
const knowledgeBases = ref<KnowledgeBase[]>([]);
const availableTools = ref<SelectableTool[]>([]);
const selectedKnowledgeBaseIds = ref<string[]>([]);
const selectedToolIds = ref<string[]>([]);
const selectedAgentId = ref("");
const selectedModelId = ref("");
const agentName = ref("New assistant");
const agentSlug = ref("new-assistant");
const systemPrompt = ref("You are a helpful assistant. Be accurate, concise and clear.");
const runInput = ref("");
const busy = ref(false);
const error = ref("");
let refreshTimer: number | undefined;

const selectedAgent = computed(
  () => agents.value.find((agent) => agent.id === selectedAgentId.value) ?? null,
);
const selectedModel = computed(
  () => models.value.find((model) => model.id === selectedModelId.value) ?? null,
);

async function loadWorkspace(): Promise<void> {
  agents.value = [];
  models.value = [];
  runs.value = [];
  knowledgeBases.value = [];
  availableTools.value = [];
  selectedKnowledgeBaseIds.value = [];
  selectedToolIds.value = [];
  selectedAgentId.value = "";
  selectedModelId.value = "";
  if (!props.tenantId || !props.projectId) return;
  error.value = "";
  try {
    const [agentList, providers] = await Promise.all([
      getAgents(props.tenantId, props.projectId),
      getModelProviders(props.tenantId),
    ]);
    agents.value = agentList;
    if (agents.value.length > 0) selectedAgentId.value = agents.value[0].id;
    const modelPages = await Promise.all(
      providers.map((provider) => getModelConfigs(props.tenantId, provider.id)),
    );
    models.value = modelPages.flat().filter((model) => model.capabilities.includes("chat"));
    if (models.value.length > 0) selectedModelId.value = models.value[0].id;
    const [basePage, catalogPage, installationPage] = await Promise.all([
      getKnowledgeBases(props.tenantId, props.projectId),
      getPluginCatalog(props.tenantId),
      getPluginInstallations(props.tenantId, props.projectId),
    ]);
    knowledgeBases.value = basePage.results;
    const installedPluginIds = new Set(
      installationPage.results
        .filter((installation) => installation.status === "installed")
        .map((installation) => installation.plugin_id),
    );
    availableTools.value = catalogPage.results
      .filter((plugin) => installedPluginIds.has(plugin.id) && plugin.enabled)
      .flatMap((plugin) =>
        plugin.tools
          .filter((tool) => tool.enabled)
          .map((tool) => ({
            ...tool,
            plugin_name: plugin.name,
            plugin_key: plugin.plugin_key,
          })),
      );
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load Agent Studio data.";
  }
}

async function loadRuns(): Promise<void> {
  if (!selectedAgentId.value) {
    runs.value = [];
    return;
  }
  try {
    runs.value = await getAgentRuns(selectedAgentId.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load Agent runs.";
  }
}

async function createDraft(): Promise<void> {
  if (!props.tenantId || !props.projectId || !selectedModel.value) return;
  busy.value = true;
  error.value = "";
  try {
    const agent = await createAgent(props.tenantId, props.projectId, {
      name: agentName.value.trim(),
      slug: agentSlug.value.trim(),
      description: "Created in AI Agent Studio",
      draft_config: {
        model_config_id: selectedModel.value.id,
        system_prompt: systemPrompt.value,
        tool_allowlist: selectedToolIds.value,
        knowledge_base_ids: selectedKnowledgeBaseIds.value,
        max_iterations: selectedToolIds.value.length > 0 ? 4 : 1,
        timeout_seconds: 60,
        max_tokens: Math.min(2048, selectedModel.value.max_output_tokens),
      },
    });
    agents.value = [...agents.value, agent];
    selectedAgentId.value = agent.id;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create agent draft.";
  } finally {
    busy.value = false;
  }
}

async function publishCurrent(): Promise<void> {
  if (!selectedAgent.value) return;
  busy.value = true;
  error.value = "";
  try {
    const version = await publishAgent(selectedAgent.value.id);
    selectedAgent.value.current_version = version.version;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to publish agent.";
  } finally {
    busy.value = false;
  }
}

async function runCurrent(): Promise<void> {
  if (!selectedAgent.value || !runInput.value.trim()) return;
  busy.value = true;
  error.value = "";
  try {
    await createAgentRun(selectedAgent.value.id, runInput.value.trim());
    runInput.value = "";
    await loadRuns();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to start Agent run.";
  } finally {
    busy.value = false;
  }
}

watch(selectedAgentId, () => void loadRuns());
watch(
  () => [props.tenantId, props.projectId],
  () => void loadWorkspace(),
);
onMounted(() => {
  void loadWorkspace();
  refreshTimer = window.setInterval(() => void loadRuns(), 3000);
});
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
});
</script>

<template>
  <section class="agent-studio">
    <div class="task-panel-heading">
      <div>
        <p class="eyebrow">AGENT BUILDER</p>
        <h3>Agents</h3>
      </div>
      <el-tag type="info" effect="plain">{{ agents.length }} drafts</el-tag>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="true" show-icon @close="error = ''" />
    <el-empty v-if="!projectId" description="Select a project to build agents" :image-size="72" />
    <template v-else>
      <div class="agent-builder-grid">
        <div class="agent-builder-form">
          <h4>Create agent draft</h4>
          <el-input v-model="agentName" placeholder="Agent name" />
          <el-input v-model="agentSlug" placeholder="URL slug" />
          <el-select v-model="selectedModelId" placeholder="Select chat model" class="full-width">
            <el-option
              v-for="model in models"
              :key="model.id"
              :label="`${model.display_name} · v${model.version}`"
              :value="model.id"
            />
          </el-select>
          <el-input v-model="systemPrompt" type="textarea" :rows="4" placeholder="System prompt" />
          <el-select
            v-model="selectedKnowledgeBaseIds"
            multiple
            collapse-tags
            placeholder="Attach project knowledge bases (optional)"
            class="full-width"
          >
            <el-option
              v-for="item in knowledgeBases"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
          <el-select
            v-model="selectedToolIds"
            multiple
            collapse-tags
            placeholder="Allow installed connector tools (optional)"
            class="full-width"
          >
            <el-option
              v-for="tool in availableTools"
              :key="tool.id"
              :label="tool.plugin_name + ' / ' + tool.name + ' · ' + tool.risk_level"
              :value="tool.id"
            />
          </el-select>
          <p class="task-note">
            Function calling is bounded to 4 turns. Tools without a registered trusted handler fail closed.
          </p>
          <el-button
            type="primary"
            :disabled="!selectedModel || (selectedToolIds.length > 0 && !selectedModel.capabilities.includes('tools'))"
            :loading="busy"
            @click="createDraft"
          >Create draft</el-button>
        </div>
        <div class="agent-builder-form">
          <h4>Publish and run</h4>
          <el-select v-model="selectedAgentId" placeholder="Select agent" class="full-width">
            <el-option
              v-for="agent in agents"
              :key="agent.id"
              :label="`${agent.name} · v${agent.current_version}`"
              :value="agent.id"
            />
          </el-select>
          <div class="agent-publish-row">
            <span>Published version</span>
            <el-tag :type="selectedAgent?.current_version ? 'success' : 'info'">
              {{ selectedAgent?.current_version ? `v${selectedAgent.current_version}` : "Draft only" }}
            </el-tag>
            <el-button
              type="primary"
              plain
              :disabled="!selectedAgent"
              :loading="busy"
              @click="publishCurrent"
            >Publish version</el-button>
          </div>
          <el-input v-model="runInput" type="textarea" :rows="3" placeholder="Enter a message to run this published agent" />
          <el-button
            type="success"
            :disabled="!selectedAgent || !selectedAgent.current_version || !runInput.trim()"
            :loading="busy"
            @click="runCurrent"
          >Run published agent</el-button>
          <p class="task-note">Published Agents can use pinned Knowledge Bases and project-installed tools. Retrieved text is untrusted context; high-risk tool approvals and connector transports remain pending.</p>
        </div>
      </div>
      <div v-if="runs.length" class="agent-run-list">
        <div class="agent-run-heading">
          <h4>Recent runs</h4>
          <span class="task-note">Auto-refresh every 3 seconds</span>
        </div>
        <article v-for="run in runs" :key="run.id" class="agent-run-card">
          <div class="agent-run-meta">
            <el-tag
              :type="run.task_status === 'succeeded' ? 'success' : run.task_status === 'failed' ? 'danger' : run.task_status === 'running' ? 'warning' : 'info'"
              size="small"
            >{{ run.task_status }}</el-tag>
            <span>{{ run.model_name || "Waiting for worker" }}</span>
            <span v-if="run.finished_at">{{ run.latency_ms }} ms</span>
          </div>
          <p class="agent-run-input">{{ run.input_text }}</p>
          <p v-if="run.output_text" class="agent-run-output">{{ run.output_text }}</p>
        </article>
      </div>
      <el-empty v-else description="No Agent runs yet" :image-size="72" />
    </template>
  </section>
</template>
