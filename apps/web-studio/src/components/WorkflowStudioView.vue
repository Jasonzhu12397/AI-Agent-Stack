<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  createWorkflow,
  createWorkflowRun,
  getWorkflowRuns,
  getWorkflows,
  publishWorkflow,
  validateWorkflow,
  updateWorkflow,
  type WorkflowDraft,
  type WorkflowRun,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();
const workflows = ref<WorkflowDraft[]>([]);
const selectedWorkflowId = ref("");
const selectedWorkflow = ref<WorkflowDraft | null>(null);
const workflowName = ref("");
const workflowDescription = ref("");
const definitionText = ref("");
const runInputText = ref('{"message":"Hello from Workflow Studio"}');
const runs = ref<WorkflowRun[]>([]);
const loading = ref(false);
const saving = ref(false);
const publishing = ref(false);
const running = ref(false);
const error = ref("");
const notice = ref("");
const validating = ref(false);
const validationResult = ref<{
  is_valid: boolean;
  errors: string[];
  execution_layers: string[][];
  node_count: number;
  edge_count: number;
} | null>(null);

const previewGraph = computed(() => {
  try {
    const parsed = JSON.parse(definitionText.value) as Record<string, unknown>;
    return {
      nodes: Array.isArray(parsed.nodes) ? parsed.nodes as Record<string, unknown>[] : [],
      edges: Array.isArray(parsed.edges) ? parsed.edges as Record<string, unknown>[] : [],
      parseError: "",
    };
  } catch {
    return { nodes: [], edges: [], parseError: "The draft is not valid JSON yet." };
  }
});

function nodeLabel(node: Record<string, unknown>): string {
  return String(node.name ?? node.id ?? "Unnamed node");
}

function nodeType(node: Record<string, unknown>): string {
  return String(node.type ?? "unknown").toUpperCase();
}

function nodeDetail(node: Record<string, unknown>): string {
  if (!node.config || typeof node.config !== "object" || Array.isArray(node.config)) return "";
  const config = node.config as Record<string, unknown>;
  return String(config.tool_id ?? config.model_config_id ?? "");
}

function edgeLabel(edge: Record<string, unknown>): string {
  return String(edge.source ?? "?") + " → " + String(edge.target ?? "?");
}

async function validateDraft(): Promise<void> {
  if (!selectedWorkflowId.value) return;
  validating.value = true;
  error.value = "";
  validationResult.value = null;
  try {
    validationResult.value = await validateWorkflow(selectedWorkflowId.value);
    if (validationResult.value.is_valid) {
      notice.value = "DAG validation passed. " + validationResult.value.node_count + " nodes and " + validationResult.value.edge_count + " edges.";
    } else {
      error.value = validationResult.value.errors.join("; ");
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Workflow validation failed.";
  } finally {
    validating.value = false;
  }
}
let refreshTimer: number | undefined;

function initialDefinition(): Record<string, unknown> {
  return {
    nodes: [
      { id: "start", type: "start", config: {} },
      { id: "end", type: "end", config: {} },
    ],
    edges: [{ source: "start", target: "end" }],
  };
}

function selectWorkflow(workflowId: string): void {
  selectedWorkflowId.value = workflowId;
  selectedWorkflow.value =
    workflows.value.find((item) => item.id === workflowId) ?? null;
  if (selectedWorkflow.value) {
    workflowName.value = selectedWorkflow.value.name;
    workflowDescription.value = selectedWorkflow.value.description;
    definitionText.value = JSON.stringify(
      selectedWorkflow.value.draft_definition,
      null,
      2,
    );
  } else {
    definitionText.value = "";
  }
  runs.value = [];
  validationResult.value = null;
  if (workflowId) void loadRuns();
}

async function loadWorkflows(): Promise<void> {
  if (!props.tenantId || !props.projectId) {
    workflows.value = [];
    selectedWorkflow.value = null;
    selectedWorkflowId.value = "";
    runs.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    workflows.value = await getWorkflows(props.tenantId, props.projectId);
    const current = workflows.value.find(
      (item) => item.id === selectedWorkflowId.value,
    );
    if (current) {
      selectWorkflow(current.id);
    } else if (workflows.value.length > 0) {
      selectWorkflow(workflows.value[0].id);
    } else {
      selectWorkflow("");
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load workflows.";
  } finally {
    loading.value = false;
  }
}

async function loadRuns(): Promise<void> {
  if (!selectedWorkflowId.value) return;
  try {
    runs.value = await getWorkflowRuns(selectedWorkflowId.value);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load workflow runs.";
  }
}

async function createDraft(): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  saving.value = true;
  error.value = "";
  notice.value = "";
  try {
    const created = await createWorkflow(props.tenantId, props.projectId, {
      name: "New workflow",
      slug: "workflow-" + crypto.randomUUID().slice(0, 8),
      description: "Created in Workflow Studio",
      draft_definition: initialDefinition(),
    });
    workflows.value = [...workflows.value, created];
    selectWorkflow(created.id);
    notice.value = "Draft created. Configure the workflow definition and save it.";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create workflow.";
  } finally {
    saving.value = false;
  }
}

function parseDefinition(): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(definitionText.value);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error("Workflow definition must be a JSON object.");
    }
    return parsed as Record<string, unknown>;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Invalid JSON definition.";
    return null;
  }
}

async function saveDraft(): Promise<void> {
  if (!selectedWorkflowId.value) return;
  const definition = parseDefinition();
  if (!definition) return;
  saving.value = true;
  error.value = "";
  notice.value = "";
  try {
    const updated = await updateWorkflow(selectedWorkflowId.value, {
      name: workflowName.value.trim(),
      description: workflowDescription.value.trim(),
      draft_definition: definition,
    });
    workflows.value = workflows.value.map((item) =>
      item.id === updated.id ? updated : item,
    );
    selectedWorkflow.value = updated;
    validationResult.value = null;
    notice.value = "Draft saved. Published versions remain immutable.";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to save workflow.";
  } finally {
    saving.value = false;
  }
}

async function publishDraft(): Promise<void> {
  if (!selectedWorkflowId.value) return;
  await saveDraft();
  if (error.value) return;
  publishing.value = true;
  error.value = "";
  notice.value = "";
  try {
    const version = await publishWorkflow(selectedWorkflowId.value);
    await loadWorkflows();
    notice.value = "Published immutable version v" + version.version + ".";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Workflow publish failed.";
  } finally {
    publishing.value = false;
  }
}

async function runWorkflow(): Promise<void> {
  if (!selectedWorkflowId.value || !selectedWorkflow.value?.current_version) return;
  let input: unknown;
  try {
    input = JSON.parse(runInputText.value);
  } catch {
    error.value = "Run input must be valid JSON.";
    return;
  }
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    error.value = "Run input must be a JSON object.";
    return;
  }
  running.value = true;
  error.value = "";
  notice.value = "";
  try {
    const run = await createWorkflowRun(
      selectedWorkflowId.value,
      input as Record<string, unknown>,
    );
    runs.value = [run, ...runs.value.filter((item) => item.id !== run.id)];
    notice.value = "Workflow run queued: " + run.id.slice(0, 8) + ".";
    await loadRuns();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to start workflow.";
  } finally {
    running.value = false;
  }
}

watch(
  () => [props.tenantId, props.projectId],
  () => void loadWorkflows(),
);

onMounted(() => {
  void loadWorkflows();
  refreshTimer = window.setInterval(() => void loadRuns(), 5000);
});
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
});
</script>

<template>
  <section class="workflow-studio">
    <div class="workflow-heading">
      <div>
        <p class="eyebrow">WORKFLOW ORCHESTRATION</p>
        <h3>Workflow Studio</h3>
      </div>
      <el-button type="primary" plain :disabled="!props.projectId" :loading="saving" @click="createDraft">
        ＋ New workflow
      </el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="notice" :title="notice" type="success" :closable="true" @close="notice = ''" />
    <el-empty v-if="!props.projectId" description="Select a project to manage workflows" :image-size="72" />
    <el-empty v-else-if="!loading && workflows.length === 0" description="Create a workflow draft to get started" :image-size="72" />
    <template v-else-if="selectedWorkflow">
      <div class="workflow-selector">
        <el-select :model-value="selectedWorkflowId" placeholder="Select workflow" class="full-width" @change="selectWorkflow">
          <el-option
            v-for="workflow in workflows"
            :key="workflow.id"
            :label="workflow.name + ' · v' + workflow.current_version"
            :value="workflow.id"
          />
        </el-select>
        <el-tag type="info" effect="plain">
          {{ selectedWorkflow.is_archived ? "ARCHIVED" : selectedWorkflow.current_version ? "PUBLISHED v" + selectedWorkflow.current_version : "DRAFT ONLY" }}
        </el-tag>
      </div>
      <section class="workflow-pipeline">
        <div class="workflow-run-heading">
          <div>
            <h4>Workflow DAG</h4>
            <p class="task-note">Live visual preview of the editable draft. Server validation remains authoritative.</p>
          </div>
          <el-tag type="info" effect="plain">{{ previewGraph.nodes.length }} nodes · {{ previewGraph.edges.length }} edges</el-tag>
        </div>
        <el-alert v-if="previewGraph.parseError" :title="previewGraph.parseError" type="warning" :closable="false" />
        <div v-else class="workflow-pipeline-row">
          <template v-for="(node, index) in previewGraph.nodes" :key="String(node.id ?? index)">
            <article class="workflow-node-card">
              <el-tag size="small" effect="plain">{{ nodeType(node) }}</el-tag>
              <strong>{{ nodeLabel(node) }}</strong>
              <code>{{ String(node.id ?? "no-id") }}</code>
              <small v-if="node.config && typeof node.config === 'object'">
                {{ nodeDetail(node) }}
              </small>
            </article>
            <span v-if="index < previewGraph.nodes.length - 1" class="workflow-node-arrow">→</span>
          </template>
        </div>
        <div v-if="previewGraph.edges.length" class="workflow-edge-list">
          <span v-for="(edge, index) in previewGraph.edges" :key="index">{{ edgeLabel(edge) }}</span>
        </div>
        <div v-if="validationResult" class="workflow-validation-result">
          <div class="workflow-run-heading">
            <strong>Server validation</strong>
            <el-tag :type="validationResult.is_valid ? 'success' : 'danger'">
              {{ validationResult.is_valid ? "VALID" : "INVALID" }}
            </el-tag>
          </div>
          <div v-if="validationResult.is_valid" class="workflow-edge-list">
            <span v-for="(layer, index) in validationResult.execution_layers" :key="index">
              Layer {{ index + 1 }}: {{ layer.join(", ") }}
            </span>
          </div>
          <el-alert v-for="(item, index) in validationResult.errors" :key="index" :title="item" type="error" :closable="false" />
        </div>
        <div class="workflow-actions">
          <el-button :loading="validating" :disabled="!selectedWorkflowId" @click="validateDraft">Validate DAG</el-button>
        </div>
      </section>
      <div class="workflow-editor-grid">
        <div class="workflow-editor-form">
          <h4>Definition</h4>
          <el-input v-model="workflowName" placeholder="Workflow name" />
          <el-input v-model="workflowDescription" placeholder="Description" />
          <el-input v-model="definitionText" type="textarea" :rows="16" resize="vertical" spellcheck="false" />
          <p class="workflow-help">
            Supported execution nodes: start, llm, knowledge_search, condition and end. Knowledge search retrieves only from the pinned project knowledge bases.
            The server validates graph connectivity, cycles, model scope and credential fields before publishing.
          </p>
          <div class="workflow-actions">
            <el-button :loading="saving" @click="saveDraft">Save draft</el-button>
            <el-button type="primary" :loading="publishing" :disabled="selectedWorkflow.is_archived" @click="publishDraft">
              Publish version
            </el-button>
          </div>
        </div>
        <div class="workflow-runner">
          <h4>Run published version</h4>
          <el-input v-model="runInputText" type="textarea" :rows="5" resize="vertical" spellcheck="false" />
          <el-button type="success" :loading="running" :disabled="!selectedWorkflow.current_version" @click="runWorkflow">
            Run workflow
          </el-button>
          <div class="workflow-run-list">
            <div class="workflow-run-heading">
              <h4>Execution history</h4>
              <el-button text @click="loadRuns">Refresh</el-button>
            </div>
            <el-empty v-if="runs.length === 0" description="No workflow runs yet" :image-size="56" />
            <article v-for="run in runs" :key="run.id" class="workflow-run-card">
              <div class="workflow-run-meta">
                <code>{{ run.id.slice(0, 8) }}</code>
                <el-tag size="small" :type="run.task_status === 'succeeded' ? 'success' : run.task_status === 'failed' ? 'danger' : 'info'">
                  {{ run.task_status }}
                </el-tag>
                <span>{{ new Date(run.created_at).toLocaleString() }}</span>
              </div>
              <pre class="workflow-run-output">{{ run.output_data === null ? "No output yet" : JSON.stringify(run.output_data, null, 2) }}</pre>
              <div class="workflow-step-list">
                <span v-for="step in run.steps" :key="step.id">{{ step.node_id }} · {{ step.status }}</span>
              </div>
            </article>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>
