<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";

type Health = { status: string; database?: string };
const apiBase = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
const apiStatus = ref("Checking");
const databaseStatus = ref("Checking");
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

onMounted(() => {
  void checkHealth();
  timer = window.setInterval(() => void checkHealth(), 10000);
});
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
});
</script>

<template>
  <main class="shell">
    <header class="topbar">
      <a class="brand" href="/"><span class="brand-mark">A</span><span>AI Agent <strong>Studio</strong></span></a>
      <el-tag effect="plain" type="info">M0 · DEVELOPMENT</el-tag>
    </header>
    <section class="hero">
      <p class="eyebrow">AGENT PLATFORM</p>
      <h1>Build, orchestrate<br /><span>and govern AI agents.</span></h1>
      <p class="intro">A modular workspace for model routing, workflow orchestration and policy-governed tool execution.</p>
      <div class="hero-actions">
        <a class="el-button el-button--primary el-button--large" :href="`${apiBase}/api/v1/`">Explore API ↗</a>
        <a class="el-button el-button--default el-button--large" href="https://github.com/Jasonzhu12397/AI-Agent-Stack">Source repository ↗</a>
      </div>
    </section>
    <section class="overview">
      <div class="section-heading">
        <div><p class="eyebrow">SYSTEM OVERVIEW</p><h2>Platform status</h2></div>
        <el-tag type="success" effect="plain">LIVE CHECK</el-tag>
      </div>
      <div class="status-card">
        <div class="status-row"><span>Platform API</span><el-tag :type="apiStatus === 'Online' ? 'success' : 'warning'">{{ apiStatus }}</el-tag></div>
        <div class="status-row"><span>PostgreSQL</span><el-tag :type="databaseStatus === 'Ready' ? 'success' : 'warning'">{{ databaseStatus }}</el-tag></div>
        <div class="status-row"><span>Worker</span><el-tag type="info">Heartbeat service</el-tag></div>
      </div>
    </section>
    <section class="milestones">
      <p class="eyebrow">DELIVERY ROADMAP</p>
      <div class="milestone-grid">
        <article v-for="(item, index) in [
          { code: 'M0', title: 'Platform foundation', detail: 'API, Worker, storage and developer environment' },
          { code: 'M1', title: 'Agent runtime', detail: 'Agent versions, asynchronous runs and tool routing' },
          { code: 'M2', title: 'Workflow engine', detail: 'Visual DAG authoring and validated execution' },
          { code: 'M3', title: 'Human approval', detail: 'Policy decisions and hash-bound approvals' }
        ]" :key="item.code" class="milestone">
          <span class="milestone-code" :class="{ current: index === 0 }">{{ item.code }}</span>
          <h3>{{ item.title }}</h3><p>{{ item.detail }}</p>
          <span class="milestone-state">{{ index === 0 ? "IN PROGRESS" : "PLANNED" }}</span>
        </article>
      </div>
    </section>
    <footer><span>AI Agent Studio <span class="muted">·</span> M0 foundation</span><span>Policy first. Observable by design.</span></footer>
  </main>
</template>
