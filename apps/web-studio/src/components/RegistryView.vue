<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { t, tf } from "../i18n";
import {
  getRegistryImages,
  getRegistryPullPlan,
  getRegistrySummary,
  getRegistryImageEvents,
  promoteRegistryImage,
  registerRegistryImage,
  scanRegistryImage,
  signRegistryImage,
  type RegistryCategory,
  type RegistryEvent,
  type RegistryImage,
  type RegistryPullPlan,
  type RegistrySummary,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();

const CATEGORIES: RegistryCategory[] = [
  "platform",
  "agent",
  "infrastructure",
  "kubernetes",
  "network",
  "observability",
  "workload",
];
const STAGES = ["dev", "staging", "production"] as const;

const images = ref<RegistryImage[]>([]);
const summary = ref<RegistrySummary | null>(null);
const pullPlan = ref<RegistryPullPlan | null>(null);
const events = ref<RegistryEvent[]>([]);
const eventsFor = ref("");
const loading = ref(false);
const busy = ref("");
const error = ref("");
const notice = ref("");

const categoryFilter = ref("");
const stageFilter = ref("");
const statusFilter = ref("");

const form = ref({
  name: "ai-agent-stack/eph-agent",
  digest: "",
  category: "agent" as RegistryCategory,
  source_repository: "Jasonzhu12397/AI-Agent-Stack",
});
const formOpen = ref(false);

const stageLabel = computed<Record<string, string>>(() => ({
  dev: t("Dev"),
  staging: t("Staging"),
  production: t("Production"),
}));

function shortDigest(digest: string): string {
  return digest.length > 22 ? digest.slice(0, 19) + "…" + digest.slice(-6) : digest;
}

function categoryLabel(value: string): string {
  return t(value.charAt(0).toUpperCase() + value.slice(1));
}

async function load(): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  loading.value = true;
  error.value = "";
  try {
    const filters = {
      category: categoryFilter.value,
      stage: stageFilter.value,
      status: statusFilter.value,
    };
    const [list, totals, plan] = await Promise.all([
      getRegistryImages(props.tenantId, props.projectId, filters),
      getRegistrySummary(props.tenantId, props.projectId),
      getRegistryPullPlan(props.tenantId, props.projectId, categoryFilter.value || undefined),
    ]);
    images.value = list.images;
    summary.value = totals;
    pullPlan.value = plan;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    loading.value = false;
  }
}

async function run(work: () => Promise<unknown>, label: string): Promise<void> {
  busy.value = label;
  error.value = "";
  try {
    await work();
    await load();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = "";
  }
}

function register(): void {
  if (!form.value.digest.trim()) {
    error.value = t("A digest is required: tags can move, digests cannot");
    return;
  }
  void run(
    () =>
      registerRegistryImage(props.tenantId, props.projectId, {
        name: form.value.name.trim(),
        digest: form.value.digest.trim(),
        category: form.value.category,
        source_repository: form.value.source_repository,
      }).then(() => {
        formOpen.value = false;
        notice.value = t("Image registered and queued for scanning");
      }),
    "register",
  );
}

function scan(image: RegistryImage, outcome: "passed" | "failed"): void {
  void run(
    () =>
      scanRegistryImage(props.tenantId, props.projectId, image.id, {
        scan_status: outcome,
        findings: outcome === "passed" ? 0 : 1,
      }),
    "scan-" + image.id,
  );
}

function sign(image: RegistryImage): void {
  void run(() => signRegistryImage(props.tenantId, props.projectId, image.id), "sign-" + image.id);
}

function promote(image: RegistryImage, stage: "dev" | "staging" | "production"): void {
  void run(
    () => promoteRegistryImage(props.tenantId, props.projectId, image.id, stage),
    "promote-" + image.id,
  );
}

function nextStage(image: RegistryImage): "dev" | "staging" | "production" | null {
  if (image.stage === "dev") return "staging";
  if (image.stage === "staging") return "production";
  return null;
}

async function openEvents(image: RegistryImage): Promise<void> {
  eventsFor.value = image.id;
  events.value = [];
  try {
    const response = await getRegistryImageEvents(props.tenantId, props.projectId, image.id);
    events.value = response.events;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  }
}

// The workspace resolves its tenant and project asynchronously, so the view
// reloads whenever the selection lands instead of only at mount.
watch(
  () => [props.tenantId, props.projectId],
  ([tenantId, projectId]) => {
    if (tenantId && projectId) void load();
  },
  { immediate: true },
);
</script>

<template>
  <div class="registry-view">
    <div class="registry-head">
      <div>
        <p class="eyebrow">{{ t("SUPPLY CHAIN") }}</p>
        <h3>{{ t("Central Image Registry") }}</h3>
        <p class="task-note">
          {{ t("Every image an agent, bootstrap or target cluster runs comes from this registry by digest. Public mirrors are never pulled in place.") }}
        </p>
      </div>
      <div class="registry-head-actions">
        <el-tag type="success" effect="plain">{{ summary?.registry_host ?? t("Registry host") }}</el-tag>
        <el-button :loading="loading" @click="load">{{ t("Refresh") }}</el-button>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="notice" :title="notice" type="success" :closable="true" @close="notice = ''" />

    <div class="registry-tiles">
      <div class="registry-tile">
        <span>{{ t("Images") }}</span>
        <strong>{{ summary?.total ?? 0 }}</strong>
      </div>
      <div class="registry-tile">
        <span>{{ t("Ready") }}</span>
        <strong>{{ summary?.by_status?.ready ?? 0 }}</strong>
      </div>
      <div class="registry-tile">
        <span>{{ t("Scan passed") }}</span>
        <strong>{{ summary?.scan_passed ?? 0 }}</strong>
      </div>
      <div class="registry-tile">
        <span>{{ t("Signed") }}</span>
        <strong>{{ summary?.signed ?? 0 }}</strong>
      </div>
      <div class="registry-tile">
        <span>{{ t("In production") }}</span>
        <strong>{{ summary?.by_stage?.production ?? 0 }}</strong>
      </div>
    </div>

    <div class="registry-filters">
      <el-select v-model="categoryFilter" clearable :placeholder="t('All categories')" @change="load">
        <el-option v-for="item in CATEGORIES" :key="item" :label="categoryLabel(item)" :value="item" />
      </el-select>
      <el-select v-model="stageFilter" clearable :placeholder="t('All stages')" @change="load">
        <el-option v-for="stage in STAGES" :key="stage" :label="stageLabel[stage]" :value="stage" />
      </el-select>
      <el-select v-model="statusFilter" clearable :placeholder="t('All statuses')" @change="load">
        <el-option v-for="item in ['building', 'scanning', 'ready', 'blocked']" :key="item" :label="categoryLabel(item)" :value="item" />
      </el-select>
      <span class="registry-spacer" />
      <el-button type="primary" plain @click="formOpen = !formOpen">
        ＋ {{ t("Register image") }}
      </el-button>
    </div>

    <div v-if="formOpen" class="registry-form">
      <label>
        <span>{{ t("Repository path") }}</span>
        <el-input v-model="form.name" placeholder="ai-agent-stack/eph-agent" />
      </label>
      <label>
        <span>{{ t("Digest") }}</span>
        <el-input v-model="form.digest" placeholder="sha256:…" spellcheck="false" />
      </label>
      <label>
        <span>{{ t("Category") }}</span>
        <el-select v-model="form.category">
          <el-option v-for="item in CATEGORIES" :key="item" :label="categoryLabel(item)" :value="item" />
        </el-select>
      </label>
      <label>
        <span>{{ t("Source repository") }}</span>
        <el-input v-model="form.source_repository" />
      </label>
      <div class="registry-form-actions">
        <el-button @click="formOpen = false">{{ t("Cancel") }}</el-button>
        <el-button type="primary" :loading="busy === 'register'" @click="register">
          {{ t("Register") }}
        </el-button>
      </div>
    </div>

    <div class="registry-table-card">
      <div class="workflow-run-heading">
        <h4>{{ t("Catalog") }}</h4>
        <span class="task-note">{{ tf("{0} images in scope", images.length) }}</span>
      </div>
      <el-empty v-if="!images.length && !loading" :description="t('No image registered in this project yet')" :image-size="56" />
      <table v-else class="registry-table">
        <thead>
          <tr>
            <th>{{ t("Image") }}</th>
            <th>{{ t("Digest") }}</th>
            <th>{{ t("Scan") }}</th>
            <th>{{ t("Stage") }}</th>
            <th>{{ t("Status") }}</th>
            <th>{{ t("Actions") }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="image in images" :key="image.id">
            <td>
              <strong>{{ image.name }}</strong>
              <small>{{ categoryLabel(image.category) }} · {{ image.source_repository || t("Manual") }}</small>
            </td>
            <td><code>{{ shortDigest(image.digest) }}</code></td>
            <td>
              <el-tag
                size="small"
                effect="plain"
                :type="image.scan_status === 'passed' ? 'success' : image.scan_status === 'failed' ? 'danger' : 'info'"
              >
                {{ image.scan_status }} · {{ image.scan_findings }}
              </el-tag>
              <el-tag v-if="image.signed" size="small" effect="plain" type="success">{{ t("signed") }}</el-tag>
            </td>
            <td>
              <el-tag size="small" :type="image.stage === 'production' ? 'success' : image.stage === 'staging' ? 'warning' : 'info'">
                {{ stageLabel[image.stage] }}
              </el-tag>
            </td>
            <td>{{ image.status }}</td>
            <td class="registry-actions">
              <el-button
                v-if="image.scan_status !== 'passed'"
                size="small"
                :loading="busy === 'scan-' + image.id"
                @click="scan(image, 'passed')"
              >{{ t("Scan pass") }}</el-button>
              <el-button
                v-if="image.scan_status === 'passed' && !image.signed"
                size="small"
                :loading="busy === 'sign-' + image.id"
                @click="sign(image)"
              >{{ t("Sign") }}</el-button>
              <el-button
                v-if="image.status === 'ready' && nextStage(image)"
                size="small"
                type="primary"
                plain
                :loading="busy === 'promote-' + image.id"
                @click="promote(image, nextStage(image) as 'staging' | 'production')"
              >→ {{ stageLabel[nextStage(image) as string] }}</el-button>
              <el-button size="small" text @click="openEvents(image)">{{ t("Events") }}</el-button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="eventsFor" class="registry-events">
        <div class="workflow-run-heading">
          <h4>{{ t("Image history") }}</h4>
          <el-button size="small" text @click="eventsFor = ''">{{ t("Close") }}</el-button>
        </div>
        <ul>
          <li v-for="event in events" :key="event.id">
            <strong>{{ event.action }}</strong>
            <span>{{ event.actor || "system" }}</span>
            <code>{{ new Date(event.created_at).toLocaleString() }}</code>
          </li>
        </ul>
      </div>
    </div>

    <div class="registry-pull">
      <div class="workflow-run-heading">
        <h4>{{ t("Pull plan for agents and clusters") }}</h4>
        <el-tag size="small" effect="plain" :type="pullPlan?.digest_pinned ? 'success' : 'info'">
          {{ pullPlan?.digest_pinned ? t("Digest pinned") : t("Not pinned") }}
        </el-tag>
      </div>
      <p class="task-note">
        {{ t("EPH bootstrap and target clusters read this list and pull from the internal host only.") }}
      </p>
      <pre class="registry-pull-code">{{ (pullPlan?.images ?? []).map((item) => item.internal_ref).join("\n") || t("No ready image to pull yet") }}</pre>
    </div>
  </div>
</template>

<style scoped>
.registry-view { display: grid; gap: 14px; min-width: 0; max-width: 100%; }
.registry-view > * { min-width: 0; }
.registry-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
.registry-head h3 { margin: 0; font-size: 20px; }
.registry-head .task-note { max-width: 720px; }
.registry-head-actions { display: flex; align-items: center; gap: 8px; }
.registry-tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
.registry-tile { display: grid; gap: 4px; padding: 14px 16px; background: #ffffff; border: 1px solid #e3eaf2; border-radius: 12px; }
.registry-tile span { color: #566976; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
.registry-tile strong { font-size: 22px; color: #121f28; }
.registry-filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.registry-filters .el-select { width: 180px; }
.registry-spacer { flex: 1; }
.registry-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; padding: 16px; background: #fbfdfe; border: 1px solid #e3eaf2; border-radius: 12px; }
.registry-form label { display: grid; gap: 6px; color: #566976; font-size: 11px; }
.registry-form-actions { grid-column: 1 / -1; display: flex; justify-content: flex-end; gap: 8px; }
.registry-table-card { overflow-x: auto; max-width: 100%; min-width: 0; padding: 16px; background: #ffffff; border: 1px solid #e3eaf2; border-radius: 12px; }
.registry-table { width: 100%; min-width: 680px; border-collapse: collapse; font-size: 12px; }
.registry-table th { padding: 8px 10px; color: #566976; font-size: 10px; text-align: left; text-transform: uppercase; letter-spacing: 0.06em; border-bottom: 1px solid #e3eaf2; }
.registry-table td { padding: 10px; border-bottom: 1px solid #eff3f7; vertical-align: top; }
.registry-table td small { display: block; color: #7b8a97; font-size: 10px; }
.registry-table code { color: #0a7f58; font: 11px "DM Mono", monospace; }
.registry-actions { display: flex; gap: 6px; flex-wrap: wrap; }
.registry-events ul { margin: 8px 0 0; padding: 0; list-style: none; display: grid; gap: 6px; }
.registry-events li { display: flex; gap: 10px; align-items: center; color: #566976; font-size: 11px; }
.registry-pull { padding: 16px; background: #ffffff; border: 1px solid #e3eaf2; border-radius: 12px; }
.registry-pull-code { margin: 8px 0 0; padding: 12px; overflow: auto; max-height: 180px; background: #f4f8fb; border-radius: 8px; color: #121f28; font: 11px/1.7 "DM Mono", monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 760px) {
  .registry-tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .registry-form { grid-template-columns: 1fr; }
  .registry-table th:nth-child(2), .registry-table td:nth-child(2) { display: none; }
}
</style>
