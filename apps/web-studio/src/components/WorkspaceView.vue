<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import WorkflowStudioView from "./WorkflowStudioView.vue";
import DashboardView from "./DashboardView.vue";
import IntegrationHubView from "./IntegrationHubView.vue";
import KnowledgeBaseView from "./KnowledgeBaseView.vue";
import {
  cancelTask,
  createEchoTask,
  createProject,
  createTenant,
  getProjects,
  getTasks,
  getTenants,
  type Project,
  type Task,
  type Tenant,
} from "../services/api";

const props = withDefaults(
  defineProps<{
    activeSection?: "overview" | "models" | "agents" | "workflows" | "integrations" | "knowledge";
  }>(),
  { activeSection: "overview" },
);

const tenants = ref<Tenant[]>([]);
const projects = ref<Project[]>([]);
const tasks = ref<Task[]>([]);
const selectedTenantId = ref("");
const selectedProjectId = ref("");
const tenantName = ref("");
const tenantSlug = ref("");
const projectName = ref("");
const projectSlug = ref("");
const taskMessage = ref("Hello from AI Agent Studio");
const showTenantForm = ref(false);
const showProjectForm = ref(false);
const loading = ref(false);
const submitting = ref(false);
const error = ref("");
let refreshTimer: number | undefined;

async function loadTenants(): Promise<void> {
  loading.value = true;
  try {
    const page = await getTenants();
    tenants.value = page.results;
    if (!selectedTenantId.value && tenants.value.length > 0) {
      selectedTenantId.value = tenants.value[0].id;
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load organizations.";
  } finally {
    loading.value = false;
  }
}

async function loadProjects(tenantId: string): Promise<void> {
  projects.value = [];
  tasks.value = [];
  selectedProjectId.value = "";
  if (!tenantId) return;
  try {
    const page = await getProjects(tenantId);
    projects.value = page.results;
    if (projects.value.length > 0) selectedProjectId.value = projects.value[0].id;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load projects.";
  }
}

async function loadTasks(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) {
    tasks.value = [];
    return;
  }
  try {
    const page = await getTasks(selectedTenantId.value, selectedProjectId.value);
    tasks.value = page.results;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load tasks.";
  }
}

async function submitTenant(): Promise<void> {
  submitting.value = true;
  error.value = "";
  try {
    const tenant = await createTenant(tenantName.value.trim(), tenantSlug.value.trim());
    tenants.value = [...tenants.value, tenant];
    selectedTenantId.value = tenant.id;
    tenantName.value = "";
    tenantSlug.value = "";
    showTenantForm.value = false;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create organization.";
  } finally {
    submitting.value = false;
  }
}

async function submitProject(): Promise<void> {
  if (!selectedTenantId.value) return;
  submitting.value = true;
  error.value = "";
  try {
    const project = await createProject(
      selectedTenantId.value,
      projectName.value.trim(),
      projectSlug.value.trim(),
    );
    projects.value = [...projects.value, project];
    selectedProjectId.value = project.id;
    projectName.value = "";
    projectSlug.value = "";
    showProjectForm.value = false;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create project.";
  } finally {
    submitting.value = false;
  }
}

async function enqueueEcho(): Promise<void> {
  if (!selectedTenantId.value || !selectedProjectId.value) return;
  submitting.value = true;
  error.value = "";
  try {
    await createEchoTask(
      selectedTenantId.value,
      selectedProjectId.value,
      taskMessage.value,
    );
    await loadTasks();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to enqueue task.";
  } finally {
    submitting.value = false;
  }
}

async function stopTask(taskId: string): Promise<void> {
  try {
    await cancelTask(taskId);
    await loadTasks();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to cancel task.";
  }
}

watch(selectedTenantId, (tenantId) => void loadProjects(tenantId));
watch(selectedProjectId, () => void loadTasks());

onMounted(() => {
  void loadTenants();
  refreshTimer = window.setInterval(() => void loadTasks(), 5000);
});
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer);
});
</script>

<template>
  <section class="workspace">
    <div class="workspace-heading">
      <div>
        <p class="eyebrow">CONTROL PLANE</p>
        <h2>
          {{
            props.activeSection === "overview"
              ? "Overview"
              : props.activeSection === "models"
                ? "Model Manager"
                : props.activeSection === "agents"
                  ? "Agent Studio"
                  : props.activeSection === "workflows"
                    ? "Workflow Studio"
                    : props.activeSection === "knowledge"
                      ? "Knowledge Bases"
                      : "Integration Hub"
          }}
        </h2>
      </div>
      <el-tag type="success" effect="plain">SESSION AUTHENTICATED</el-tag>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <div class="workspace-grid">
      <aside class="workspace-sidebar">
        <div class="workspace-section-heading">
          <h3>Organizations</h3>
          <el-button text type="primary" @click="showTenantForm = !showTenantForm">＋ New</el-button>
        </div>
        <el-select v-model="selectedTenantId" placeholder="Select organization" class="full-width">
          <el-option v-for="tenant in tenants" :key="tenant.id" :label="tenant.name" :value="tenant.id" />
        </el-select>
        <div v-if="showTenantForm" class="inline-form">
          <el-input v-model="tenantName" placeholder="Organization name" />
          <el-input v-model="tenantSlug" placeholder="URL slug" />
          <el-button type="primary" :loading="submitting" :disabled="!tenantName || !tenantSlug" @click="submitTenant">Create organization</el-button>
        </div>
        <el-divider />
        <div class="workspace-section-heading">
          <h3>Projects</h3>
          <el-button text type="primary" :disabled="!selectedTenantId" @click="showProjectForm = !showProjectForm">＋ New</el-button>
        </div>
        <el-select v-model="selectedProjectId" placeholder="Select project" class="full-width">
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
        <div v-if="showProjectForm" class="inline-form">
          <el-input v-model="projectName" placeholder="Project name" />
          <el-input v-model="projectSlug" placeholder="URL slug" />
          <el-button type="primary" :loading="submitting" :disabled="!projectName || !projectSlug" @click="submitProject">Create project</el-button>
        </div>
        <el-empty v-if="!loading && tenants.length === 0" description="Create your first organization" :image-size="72" />
      </aside>
      <section class="task-panel">
        <div v-if="props.activeSection === 'overview'">
          <DashboardView
            :tenant-id="selectedTenantId"
            :project-id="selectedProjectId"
          />
        <div class="task-panel-heading">
          <div>
            <p class="eyebrow">PERSISTENT EXECUTION</p>
            <h3>Task queue</h3>
          </div>
          <el-tag type="info" effect="plain">{{ tasks.length }} recent</el-tag>
        </div>
          <div v-if="selectedProjectId" class="task-create">
            <el-input v-model="taskMessage" placeholder="Message for demo.echo" @keyup.enter="enqueueEcho" />
            <el-button type="primary" :loading="submitting" @click="enqueueEcho">Run demo task</el-button>
          </div>
          <el-empty v-else description="Select or create a project to view tasks" :image-size="90" />
          <el-table v-if="selectedProjectId" :data="tasks" size="small" class="task-table">
            <el-table-column prop="task_type" label="Task" min-width="110" />
            <el-table-column label="Status" min-width="120">
              <template #default="scope">
                <el-tag
                  :type="scope.row.status === 'succeeded' ? 'success' : scope.row.status === 'failed' ? 'danger' : scope.row.status === 'running' ? 'warning' : 'info'"
                  size="small"
                >{{ scope.row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="attempts" label="Attempts" width="86" />
            <el-table-column label="Action" width="100">
              <template #default="scope">
                <el-button
                  v-if="['queued', 'running', 'cancel_requested', 'waiting_approval'].includes(scope.row.status)"
                  text
                  type="danger"
                  size="small"
                  @click="stopTask(scope.row.id)"
                >Cancel</el-button>
              </template>
            </el-table-column>
          </el-table>
          <p class="task-note">PostgreSQL-backed tasks support lease-based Worker execution, single-turn LLM Agent runs and the current basic Workflow DAG runner.</p>
        </div>
        <ModelManagerView v-if="props.activeSection === 'models'" :tenant-id="selectedTenantId" />
        <AgentStudioView
          v-if="props.activeSection === 'agents'"
          :tenant-id="selectedTenantId"
          :project-id="selectedProjectId"
        />
        <WorkflowStudioView
          v-if="props.activeSection === 'workflows'"
          :tenant-id="selectedTenantId"
          :project-id="selectedProjectId"
        />
        <IntegrationHubView
          v-if="props.activeSection === 'integrations'"
          :tenant-id="selectedTenantId"
          :project-id="selectedProjectId"
        />
        <KnowledgeBaseView
          v-if="props.activeSection === 'knowledge'"
          :tenant-id="selectedTenantId"
          :project-id="selectedProjectId"
        />
      </section>
    </div>
  </section>
</template>
