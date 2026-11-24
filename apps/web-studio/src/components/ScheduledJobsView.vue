<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { t, tf } from "../i18n";
import {
  createScheduledJob,
  deleteScheduledJob,
  getScheduledJobs,
  runScheduledJobNow,
  updateScheduledJob,
  type ScheduledJob,
} from "../services/api";
import { activeProjectId, activeTenantId, projects, refreshThreads } from "../state/workspace";

const router = useRouter();
const jobs = ref<ScheduledJob[]>([]);
const loading = ref(false);
const error = ref("");
const notice = ref("");
const busy = ref("");
const dialogOpen = ref(false);

const form = ref({
  name: "",
  prompt: "",
  cadence: "daily" as "hourly" | "daily" | "weekly",
  hour: 9,
  weekday: 0,
  execution_target: "cloud" as "cloud" | "local",
  effort: "auto" as "auto" | "low",
});

const hasWorkspace = computed(() => Boolean(activeTenantId.value && activeProjectId.value));
const projectName = computed(
  () => projects.value.find((item) => item.id === activeProjectId.value)?.name ?? "",
);

const CADENCE_LABELS: Record<string, string> = {
  hourly: "每小时",
  daily: "每天",
  weekly: "每周",
};

async function load(): Promise<void> {
  if (!hasWorkspace.value) {
    jobs.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    jobs.value = (await getScheduledJobs(activeTenantId.value, activeProjectId.value)).items;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    loading.value = false;
  }
}

async function create(): Promise<void> {
  if (!form.value.name.trim() || !form.value.prompt.trim()) {
    error.value = t("A schedule needs a name and a prompt");
    return;
  }
  busy.value = "create";
  try {
    const created = await createScheduledJob(activeTenantId.value, activeProjectId.value, {
      name: form.value.name.trim(),
      prompt: form.value.prompt.trim(),
      cadence: form.value.cadence,
      hour: form.value.hour,
      weekday: form.value.weekday,
      execution_target: form.value.execution_target,
      effort: form.value.effort,
    });
    jobs.value = [...jobs.value, created];
    dialogOpen.value = false;
    form.value.name = "";
    form.value.prompt = "";
    notice.value = t("Schedule created");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = "";
  }
}

async function toggle(job: ScheduledJob): Promise<void> {
  busy.value = job.id;
  try {
    const updated = await updateScheduledJob(activeTenantId.value, activeProjectId.value, job.id, {
      enabled: !job.enabled,
    });
    jobs.value = jobs.value.map((item) => (item.id === job.id ? updated : item));
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = "";
  }
}

async function runNow(job: ScheduledJob): Promise<void> {
  busy.value = job.id;
  error.value = "";
  try {
    const updated = await runScheduledJobNow(activeTenantId.value, activeProjectId.value, job.id);
    jobs.value = jobs.value.map((item) => (item.id === job.id ? updated : item));
    await refreshThreads();
    notice.value = t("Turn executed; open the conversation to read it");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = "";
  }
}

async function remove(job: ScheduledJob): Promise<void> {
  busy.value = job.id;
  try {
    await deleteScheduledJob(activeTenantId.value, activeProjectId.value, job.id);
    jobs.value = jobs.value.filter((item) => item.id !== job.id);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : String(cause);
  } finally {
    busy.value = "";
  }
}

function openThread(job: ScheduledJob): void {
  if (!job.thread_id) return;
  void router.push("/chat");
  // The rail and chat share the selection store, so the thread opens there.
  window.dispatchEvent(
    new CustomEvent("agent-stack:open-thread", { detail: { threadId: job.thread_id } }),
  );
}

function scheduleText(job: ScheduledJob): string {
  if (job.cadence === "hourly") return t("Every hour");
  const hour = String(job.hour).padStart(2, "0") + ":00";
  if (job.cadence === "daily") return `${t("Daily at")} ${hour}`;
  const weekday = [t("Monday"), t("Tuesday"), t("Wednesday"), t("Thursday"), t("Friday"), t("Saturday"), t("Sunday")][job.weekday] ?? "";
  return `${weekday} ${hour}`;
}

function timeText(value: string | null): string {
  if (!value) return t("never");
  return new Date(value).toLocaleString();
}

watch([activeTenantId, activeProjectId], () => void load());
onMounted(() => void load());
</script>

<template>
  <div class="scheduled-view">
    <header class="scheduled-head">
      <div>
        <p class="eyebrow">CONTROL AGENT</p>
        <h2>{{ t("Scheduled tasks") }}</h2>
        <p class="task-note">
          {{ t("A schedule is a prompt plus a cadence. The worker runs the turn on time and writes it into a conversation of the project.") }}
        </p>
      </div>
      <div class="scheduled-actions">
        <el-tag size="small" effect="plain">{{ projectName || t("No project") }}</el-tag>
        <el-button :loading="loading" @click="load">{{ t("Refresh") }}</el-button>
        <el-button type="primary" @click="dialogOpen = true">＋ {{ t("New schedule") }}</el-button>
      </div>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="notice" :title="notice" type="success" :closable="true" @close="notice = ''" />

    <div v-if="!jobs.length && !loading" class="scheduled-empty">
      <p>{{ t("No schedule in this project yet.") }}</p>
      <el-button type="primary" plain @click="dialogOpen = true">{{ t("Create the first schedule") }}</el-button>
    </div>

    <div v-else class="scheduled-list">
      <article v-for="job in jobs" :key="job.id" class="scheduled-card">
        <div class="scheduled-card-main">
          <div class="scheduled-title">
            <strong>{{ job.name }}</strong>
            <el-tag size="small" :type="job.enabled ? 'success' : 'info'" effect="plain">
              {{ job.enabled ? t("Active") : t("Paused") }}
            </el-tag>
            <el-tag v-if="job.last_error_code" size="small" type="danger" effect="plain">
              {{ job.last_error_code }}
            </el-tag>
          </div>
          <p class="scheduled-prompt">{{ job.prompt }}</p>
          <div class="scheduled-meta">
            <span>◷ {{ scheduleText(job) }}</span>
            <span>{{ t("Next run") }}: {{ timeText(job.next_run_at) }}</span>
            <span>{{ t("Last run") }}: {{ timeText(job.last_run_at) }}</span>
          </div>
        </div>
        <div class="scheduled-card-actions">
          <el-button size="small" :loading="busy === job.id" @click="runNow(job)">{{ t("Run now") }}</el-button>
          <el-button size="small" @click="openThread(job)" :disabled="!job.thread_id">{{ t("Open conversation") }}</el-button>
          <el-button size="small" text @click="toggle(job)">{{ job.enabled ? t("Pause") : t("Resume") }}</el-button>
          <el-button size="small" text type="danger" @click="remove(job)">{{ t("Delete") }}</el-button>
        </div>
      </article>
    </div>

    <el-dialog v-model="dialogOpen" :title="t('New schedule')" width="560px">
      <div class="scheduled-form">
        <label>
          <span>{{ t("Name") }}</span>
          <el-input v-model="form.name" :placeholder="t('Morning fleet report')" />
        </label>
        <label>
          <span>{{ t("Prompt") }}</span>
          <el-input v-model="form.prompt" type="textarea" :rows="4" :placeholder="t('What should the Control Agent check each time?')" />
        </label>
        <div class="scheduled-form-row">
          <label>
            <span>{{ t("Cadence") }}</span>
            <el-select v-model="form.cadence">
              <el-option :label="t('Every hour')" value="hourly" />
              <el-option :label="t('Daily')" value="daily" />
              <el-option :label="t('Weekly')" value="weekly" />
            </el-select>
          </label>
          <label>
            <span>{{ t("Hour") }}</span>
            <el-input-number v-model="form.hour" :min="0" :max="23" />
          </label>
          <label v-if="form.cadence === 'weekly'">
            <span>{{ t("Weekday") }}</span>
            <el-select v-model="form.weekday">
              <el-option v-for="(label, index) in [t('Monday'), t('Tuesday'), t('Wednesday'), t('Thursday'), t('Friday'), t('Saturday'), t('Sunday')]" :key="index" :label="label" :value="index" />
            </el-select>
          </label>
        </div>
        <div class="scheduled-form-row">
          <label>
            <span>{{ t("Environment") }}</span>
            <el-select v-model="form.execution_target">
              <el-option :label="t('Cloud computer')" value="cloud" />
              <el-option :label="t('Local machine')" value="local" />
            </el-select>
          </label>
          <label>
            <span>{{ t("Reasoning") }}</span>
            <el-select v-model="form.effort">
              <el-option :label="t('Auto')" value="auto" />
              <el-option :label="t('Low')" value="low" />
            </el-select>
          </label>
        </div>
      </div>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ t("Cancel") }}</el-button>
        <el-button type="primary" :loading="busy === 'create'" @click="create">{{ t("Create") }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.scheduled-view { display: grid; gap: 14px; }
.scheduled-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
.scheduled-head h2 { margin: 2px 0 6px; font-size: 22px; }
.scheduled-actions { display: flex; align-items: center; gap: 8px; }
.scheduled-empty { padding: 40px; text-align: center; background: #fff; border: 1px solid #e3eaf2; border-radius: 12px; color: #6c7d8a; display: grid; gap: 12px; justify-items: center; }
.scheduled-list { display: grid; gap: 10px; }
.scheduled-card { display: flex; justify-content: space-between; gap: 16px; padding: 16px; background: #fff; border: 1px solid #e3eaf2; border-radius: 12px; flex-wrap: wrap; }
.scheduled-card-main { min-width: 0; flex: 1; display: grid; gap: 6px; }
.scheduled-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.scheduled-prompt { margin: 0; color: #4c5f6b; font-size: 12px; line-height: 1.6; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.scheduled-meta { display: flex; gap: 14px; flex-wrap: wrap; color: #8296a4; font-size: 11px; }
.scheduled-card-actions { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.scheduled-form { display: grid; gap: 12px; }
.scheduled-form label { display: grid; gap: 6px; color: #566976; font-size: 11px; }
.scheduled-form-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
@media (max-width: 760px) {
  .scheduled-form-row { grid-template-columns: 1fr; }
  .scheduled-card { flex-direction: column; }
}
</style>
