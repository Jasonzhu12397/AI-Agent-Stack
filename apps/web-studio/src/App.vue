<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import en from "element-plus/es/locale/lang/en";
import { locale, setLocale } from "./i18n";
import { apiBase, logout, type CurrentUser } from "./services/api";
import { currentUser, setAuthenticated, resetAuthCache } from "./router";

type Health = { status: string; database?: string };

const route = useRoute();
const router = useRouter();
const elementLocale = computed(() => (locale.value === "zh" ? zhCn : en));

const navItems = [
  { to: "/", label: "Overview" },
  { to: "/models", label: "Model Manager" },
  { to: "/agents", label: "Agent Studio" },
  { to: "/workflows", label: "Workflow DAG" },
  { to: "/integrations", label: "Integration Hub" },
  { to: "/knowledge", label: "Knowledge Bases" },
  { to: "/infrastructure", label: "Metal3 Fleet" },
  { to: "/governance", label: "Governance" },
  { to: "/audit", label: "Audit & Activity" },
  { to: "/organization", label: "Organization" },
  { to: "/docs", label: "Docs & Matrix" },
];

const apiStatus = ref("Checking");
const databaseStatus = ref("Checking");
const authError = ref("");
let timer: number | undefined;

const isLogin = computed(() => route.path === "/login");
const isSettings = computed(() => route.path.startsWith("/settings"));

function isActive(to: string): boolean {
  if (to === "/") return route.path === "/";
  return route.path === to || route.path.startsWith(`${to}/`);
}

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

async function signOut(): Promise<void> {
  authError.value = "";
  try {
    await logout();
  } catch (cause) {
    authError.value = cause instanceof Error ? cause.message : "Logout failed.";
  } finally {
    resetAuthCache();
    await router.push("/login");
  }
}

function onAuthenticated(user: CurrentUser): void {
  setAuthenticated(user);
  const next = typeof route.query.next === "string" && route.query.next ? route.query.next : "/";
  void router.push(next);
}

onMounted(() => {
  void checkHealth();
  timer = window.setInterval(() => void checkHealth(), 10000);
});
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
});
</script>

<template>
  <el-config-provider :locale="elementLocale">
    <router-view v-if="isLogin" @authenticated="onAuthenticated" />
    <main v-else class="shell">
      <header class="topbar">
        <router-link class="brand" to="/">
          <span class="brand-mark">A</span>
          <span>AI Agent <strong>Studio</strong></span>
        </router-link>
        <div class="account-actions">
          <el-tag effect="plain" type="info">DUAL-AGENT PREVIEW</el-tag>
          <span v-if="currentUser" class="account-name">{{ currentUser.username }}</span>
          <el-button-group class="locale-switch" size="small">
            <el-button :type="locale === 'en' ? 'primary' : 'default'" @click="setLocale('en')">English</el-button>
            <el-button :type="locale === 'zh' ? 'primary' : 'default'" @click="setLocale('zh')">中文</el-button>
          </el-button-group>
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
        <router-link
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          :class="{ active: isActive(item.to) }"
        >
          {{ item.label }}
        </router-link>
        <router-link to="/settings/account" :class="{ active: isSettings }">Settings</router-link>
      </nav>
      <section v-if="!isSettings" class="hero">
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
      <router-view />
      <template v-if="!isSettings">
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
      </template>
      <footer>
        <span>AI Agent Studio <span class="muted">·</span> M0 verified, M1–M3 partial</span>
        <span>Policy first. Observable by design.</span>
      </footer>
    </main>
  </el-config-provider>
</template>
