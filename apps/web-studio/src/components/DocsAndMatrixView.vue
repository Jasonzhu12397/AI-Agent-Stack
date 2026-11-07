<script setup lang="ts">
const phases = [
  { id: "P0", name: "Platform foundation", status: "verified", detail: "Django API, Vue 3 Studio, PostgreSQL, Redis and durable Worker queue." },
  { id: "P1", name: "Identity and tenancy", status: "partial", detail: "Session authentication, tenant/project membership and server-side RBAC." },
  { id: "P2", name: "Model and Agent runtime", status: "partial", detail: "OpenAI-compatible providers, published Agent versions, asynchronous runs and tool routing." },
  { id: "P3", name: "Workflow orchestration", status: "partial", detail: "Versioned DAG definitions, validation, persisted workflow runs and step records." },
  { id: "P4", name: "Plugin governance", status: "partial", detail: "Plugin registry, review, installation, permission grants and credential references. Real connector execution and OAuth broker remain incomplete." },
  { id: "P5", name: "Knowledge and MCP", status: "partial", detail: "Qdrant-backed knowledge ingestion and retrieval baseline. MCP client/server, durable memory and advanced retrieval remain incomplete." },
  { id: "P6", name: "Enterprise connectors", status: "planned", detail: "GitHub, Jira, Office 365, Teams, maps, weather and external service connectors." },
  { id: "P7", name: "Bare-metal delivery", status: "in_progress", detail: "EPH and Target Agent control, CAPI/Metal3/Ironic preflight evidence and deployment state machine. Actual provisioning and CAPI pivot are not yet enabled." },
  { id: "P8", name: "Production operations", status: "partial", detail: "Tenant-scoped append-only hash-chained audit events cover selected sensitive writes. Full event coverage, external hash anchoring, retention, metrics, tracing and backup/restore remain incomplete." },
];
const lifecycle = [
  "Boot EPH with RAM OS and a temporary single-node Kubernetes control plane.",
  "Run EPH Agent as a Pod and let Supervisor persist deployment checkpoints outside EPH.",
  "Use CAPI, Metal3 and Ironic to provision the first Target Control Plane.",
  "Start Target Agent on that Control Plane and verify its heartbeat and checkpoint recovery.",
  "Pivot CAPI management ownership only after explicit compatibility, approval and credential checks.",
  "Provision remaining Control Plane and Worker nodes from the target management plane.",
  "Reclaim EPH hardware, reinstall it as a Worker and register its new node identity.",
];
function openDocs(): void {
  window.open("https://github.com/Jasonzhu12397/AI-Agent-Stack/tree/feature/dual-agent-control-plane/docs", "_blank", "noopener");
}
function statusType(status: string): "success" | "warning" | "info" {
  if (status === "verified") return "success";
  if (status === "in_progress") return "warning";
  return "info";
}
</script>

<template>
  <section class="operations-view">
    <header class="operations-heading">
      <div><p class="eyebrow">ARCHITECTURE & DELIVERY</p><h3>Documentation & Verification Matrix</h3>
        <p>Delivery state is explicit: partial and planned capabilities are not presented as production-ready.</p>
      </div>
      <el-button plain @click="openDocs">Open repository docs ↗</el-button>
    </header>
    <section class="operations-panel">
      <div class="operations-panel-heading"><h4>Platform delivery matrix</h4><span>P0–P8</span></div>
      <div v-for="phase in phases" :key="phase.id" class="matrix-row">
        <span class="matrix-code">{{ phase.id }}</span>
        <div class="matrix-content"><strong>{{ phase.name }}</strong><p>{{ phase.detail }}</p></div>
        <el-tag :type="statusType(phase.status)" size="small">{{ phase.status.replace("_", " ") }}</el-tag>
      </div>
    </section>
    <section class="operations-panel">
      <div class="operations-panel-heading"><h4>EPH → Target Cluster → EPH reclaim lifecycle</h4><span>REQUIRED ORDER</span></div>
      <ol class="lifecycle-list"><li v-for="step in lifecycle" :key="step">{{ step }}</li></ol>
    </section>
    <section class="operations-panel">
      <h4>Engineering references</h4>
      <div class="reference-links">
        <a href="https://github.com/Jasonzhu12397/AI-Agent-Stack/blob/feature/dual-agent-control-plane/docs/dual-agent-infrastructure-architecture.md" target="_blank" rel="noreferrer">Dual-agent infrastructure architecture ↗</a>
        <a href="https://github.com/Jasonzhu12397/AI-Agent-Stack/blob/feature/dual-agent-control-plane/docs/eph-node-lifecycle.md" target="_blank" rel="noreferrer">EPH node lifecycle ↗</a>
        <a href="https://github.com/Jasonzhu12397/AI-Agent-Stack/blob/feature/dual-agent-control-plane/docs/threat-model.md" target="_blank" rel="noreferrer">Threat model ↗</a>
        <a href="https://github.com/Jasonzhu12397/AI-Agent-Stack/blob/feature/dual-agent-control-plane/docs/audit-events.md" target="_blank" rel="noreferrer">Audit event integrity model ↗</a>
        <a href="https://github.com/Jasonzhu12397/AI-Agent-Stack/blob/feature/dual-agent-control-plane/docs/agent-command-api.md" target="_blank" rel="noreferrer">Agent command history API ↗</a>
        <a href="https://github.com/Jasonzhu12397/AI-Agent-Stack/blob/feature/dual-agent-control-plane/docs/verification-matrix.md" target="_blank" rel="noreferrer">Verification matrix ↗</a>
      </div>
    </section>
  </section>
</template>
