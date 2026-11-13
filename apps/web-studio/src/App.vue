<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import LoginView from "./components/LoginView.vue";
import WorkspaceView from "./components/WorkspaceView.vue";
import Metal3FleetView from "./components/Metal3FleetView.vue";
import InfrastructureAgentsView from "./components/InfrastructureAgentsView.vue";
import {
  apiBase,
  getCurrentUser,
  logout,
  type CurrentUser,
} from "./services/api";

type Health = { status: string; database?: string };
type ActiveView = "dashboard" | "models" | "agents" | "workflows" | "integrations" | "knowledge" | "infrastructure" | "agent-nodes" | "governance" | "audit" | "organization" | "docs";
const activeView = ref<ActiveView>("dashboard");
const navItems: { id: ActiveView; label: string }[] = [
  { id: "dashboard", label: "Overview" },
  { id: "models", label: "Model Manager" },
  { id: "agents", label: "Agent Studio" },
  { id: "workflows", label: "Workflow DAG" },
  { id: "integrations", label: "Integration Hub" },
  { id: "knowledge", label: "Knowledge Bases" },
  { id: "infrastructure", label: "Metal3 Fleet" },
  { id: "agent-nodes", label: "EPH & Target Agents" },
  { id: "governance", label: "Governance" },
  { id: "audit", label: "Audit & Activity" },
  { id: "organization", label: "Organization" },
  { id: "docs", label: "Docs & Matrix" },
];
const workspaceSection = computed<
  "overview" | "models" | "agents" | "workflows" | "integrations" | "knowledge" | "governance" | "audit" | "organization" | "docs"
>(() => {
  if (activeView.value === "dashboard" || activeView.value === "infrastructure") {
    return "overview";
  }
  return activeView.value;
});
const apiStatus = ref("Checking");
const databaseStatus = ref("Checking");
const currentUser = ref<CurrentUser | null>(null);
const authLoading = ref(true);
const authError = ref("");
let timer: number | undefined;

async function checkHealth(): Promise<void> {
  try {
    const response = await fetch(`${apiBase}/health/ready`);
    const health = (await response.json()) as Health;
    apiStatus.value = response.ok ? "Online" : "Unavailable";
    databaseStatus.value = health.database === "ok" ? "Ready" : "Unavailable";
  } catch {
    apiStatus.value = "Unavailable";
    databaseStatus.value = "Unknown";
  }
}

async function loadCurrentUser(): Promise<void> {
  authError.value = "";
  try {
    currentUser.value = await getCurrentUser();
  } catch {
    currentUser.value = null;
  } finally {
    authLoading.value = false;
  }
}

function authenticated(user: CurrentUser): void {
  currentUser.value = user;
}

async function signOut(): Promise<void> {
  try {
    await logout();
    currentUser.value = null;
  } catch (cause) {
    authError.value = cause instanceof Error ? cause.message : "Logout failed.";
  }
}

onMounted(() => {
  void checkHealth();
  void loadCurrentUser();
  timer = window.setInterval(() => void checkHealth(), 10000);
});
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
});
</script>

<template>
  <LoginView v-if="!authLoading && !currentUser" @authenticated="authenticated" />
  <main v-else class="shell">
    <header class="topbar">
      <a class="brand" href="/">
        <span class="brand-mark">A</span>
        <span>AI Agent <strong>Studio</strong></span>
      </a>
      <div class="account-actions">
        <el-tag effect="plain" type="info">DUAL-AGENT PREVIEW</el-tag>
        <span v-if="currentUser" class="account-name">{{ currentUser.username }}</span>
        <el-button v-if="currentUser" text @click="signOut">Sign out</el-button>
      </div>
    </header>
    <el-alert
      v-if="authError"
      :title="authError"
      type="error"
      :closable="true"
      @close="authError = ''"
    />
    <nav class="main-nav" aria-label="Main navigation">
      <button
        v-for="item in navItems"
        :key="item.id"
        :class="{ active: activeView === item.id }"
        @click="activeView = item.id"
      >
        {{ item.label }}
      </button>
    </nav>
    <section class="hero">
      <p class="eyebrow">AGENT PLATFORM</p>
      <h1>Build, orchestrate<br /><span>and govern AI agents.</span></h1>
      <p class="intro">
        A modular workspace for model routing, workflow orchestration and
        policy-governed tool execution.
      </p>
      <div class="hero-actions">
        <a class="el-button el-button--primary el-button--large" :href="`${apiBase}/api/v1/`">
          Explore API ↗
        </a>
        <a
          class="el-button el-button--default el-button--large"
          href="https://github.com/Jasonzhu12397/AI-Agent-Stack"
        >
          Source repository ↗
        </a>
      </div>
    </section>
    <WorkspaceView
      v-if="activeView !== 'infrastructure' && activeView !== 'agent-nodes'"
      :active-section="workspaceSection"
    />
    <Metal3FleetView v-else-if="activeView === 'infrastructure'" />
    <InfrastructureAgentsView v-else />
    <section class="overview">
      <div class="section-heading">
        <div><p class="eyebrow">SYSTEM OVERVIEW</p><h2>Platform status</h2></div>
        <el-tag type="success" effect="plain">LIVE CHECK</el-tag>
      </div>
      <div class="status-card">
        <div class="status-row">
          <span>Platform API</span>
          <el-tag :type="apiStatus === 'Online' ? 'success' : 'warning'">{{ apiStatus }}</el-tag>
        </div>
        <div class="status-row">
          <span>PostgreSQL</span>
          <el-tag :type="databaseStatus === 'Ready' ? 'success' : 'warning'">
            {{ databaseStatus }}
          </el-tag>
        </div>
        <div class="status-row"><span>Worker</span><el-tag type="info">Durable PostgreSQL queue</el-tag></div>
      </div>
    </section>
    <section class="milestones">
      <p class="eyebrow">DELIVERY ROADMAP</p>
      <div class="milestone-grid">
        <article
          v-for="(item, index) in [
            { code: 'M0', title: 'Platform foundation', detail: 'Django API, Vue Studio, PostgreSQL, Worker and Compose' },
            { code: 'M1', title: 'Agent runtime', detail: 'Published Agent versions and asynchronous LLM runs' },
            { code: 'M2', title: 'Workflow engine', detail: 'Versioned DAG, LLM/Condition nodes and persisted steps' },
            { code: 'M3', title: 'Dual-agent control', detail: 'EPH and Target Cluster enrollment, command leases and approval gate' }
          ]"
          :key="item.code"
          class="milestone"
        >
          <span class="milestone-code" :class="{ current: index === 0 }">{{ item.code }}</span>
          <h3>{{ item.title }}</h3>
          <p>{{ item.detail }}</p>
          <span class="milestone-state">{{ ["VERIFIED BASELINE", "PARTIAL", "PARTIAL", "IN PROGRESS"][index] }}</span>
        </article>
      </div>
    </section>
    <footer>
      <span>AI Agent Studio <span class="muted">·</span> M0 verified, M1–M3 partial</span>
      <span>Policy first. Observable by design.</span>
    </footer>
  </main>
</template>
