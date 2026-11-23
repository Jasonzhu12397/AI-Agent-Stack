<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { activeTenantId, ensureTenantContext, tenantContextError } from "../shared/tenantContext";
import {
  createExportJob,
  expireExportJob,
  fetchDataControls,
  fetchExportJobs,
  saveDataControls,
} from "./api";
import {
  DATA_CONTROLS_DEFAULTS,
  EXPORT_FORMATS,
  EXPORT_IDEMPOTENCY_PREFIX,
  buildIdempotencyKey,
  canDownloadExport,
  describeError,
  exportGate,
  exportScopeWarning,
  exportStatusMeta,
  normalizeDataControls,
  pageLabels,
  type DataControlsValues,
  type ExportJob,
} from "./logic";

type Bundle = { envelope: Awaited<ReturnType<typeof fetchDataControls>>; jobs: ExportJob[] };

const resource = useSettingsResource<Bundle>(async () => {
  await ensureTenantContext();
  if (!activeTenantId.value) {
    throw new ApiError(400, "TENANT_CONTEXT_UNAVAILABLE", tenantContextError.value || "No tenant is available.");
  }
  const [envelope, jobs] = await Promise.all([
    fetchDataControls(activeTenantId.value),
    fetchExportJobs(activeTenantId.value),
  ]);
  return { envelope, jobs };
});

const draft = ref<DataControlsValues>({ ...DATA_CONTROLS_DEFAULTS });
const saveState = ref<"idle" | "saving" | "saved" | "error">("idle");
const saveError = ref("");

const exportDialogVisible = ref(false);
const exportConfirmed = ref(false);
const exportFormat = ref<DataControlsValues["export_format"]>("json");
const exportKey = ref("");
const exportSubmitting = ref(false);
const exportError = ref("");
const expiringJobId = ref("");

const effective = computed(() => resource.data.value?.envelope.effective ?? null);
const defaults = computed(() => resource.data.value?.envelope.defaults ?? DATA_CONTROLS_DEFAULTS);
const jobs = computed(() => resource.data.value?.jobs ?? []);
const updatedAt = computed(() => resource.data.value?.envelope.updated_at ?? null);

function hydrate(envelope: Bundle["envelope"]): void {
  draft.value = normalizeDataControls(envelope.effective ?? envelope.values ?? envelope.defaults);
  exportFormat.value = draft.value.export_format;
}

async function save(): Promise<void> {
  if (!activeTenantId.value) return;
  saveState.value = "saving";
  saveError.value = "";
  try {
    const echoed = await saveDataControls(activeTenantId.value, { ...draft.value });
    hydrate(echoed);
    saveState.value = "saved";
  } catch (cause) {
    saveState.value = "error";
    saveError.value = cause instanceof ApiError ? describeError(cause.code) : "Data controls could not be saved.";
  }
}

function openExportDialog(): void {
  exportConfirmed.value = false;
  exportError.value = "";
  exportFormat.value = draft.value.export_format;
  const suffix = `${Date.now()}-${crypto.randomUUID()}`;
  exportKey.value = buildIdempotencyKey(EXPORT_IDEMPOTENCY_PREFIX, suffix);
  exportDialogVisible.value = true;
}

const exportGateState = computed(() =>
  exportGate({ confirmed: exportConfirmed.value, idempotencyKey: exportKey.value }),
);

async function submitExport(): Promise<void> {
  if (!activeTenantId.value) return;
  if (!exportGateState.value.ok) {
    exportError.value = describeError("EXPORT_GATE_NOT_PASSED");
    return;
  }
  exportSubmitting.value = true;
  exportError.value = "";
  try {
    await createExportJob(activeTenantId.value, {
      export_format: exportFormat.value,
      confirm: exportConfirmed.value,
      idempotency_key: exportKey.value,
    });
    exportDialogVisible.value = false;
    await resource.reload();
  } catch (cause) {
    exportError.value = cause instanceof ApiError ? describeError(cause.code) : "Export request failed.";
  } finally {
    exportSubmitting.value = false;
  }
}

async function expireJob(job: ExportJob): Promise<void> {
  if (!activeTenantId.value || expiringJobId.value) return;
  expiringJobId.value = job.id;
  try {
    await expireExportJob(activeTenantId.value, job.id);
    await resource.reload();
  } catch (cause) {
    saveError.value = cause instanceof ApiError ? describeError(cause.code) : "Export could not be expired.";
  } finally {
    expiringJobId.value = "";
  }
}

onMounted(async () => {
  await resource.reload();
  if (resource.data.value) hydrate(resource.data.value.envelope);
});
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Data Controls"
    description="Retention period, export format and export approval. Values come from the server envelope; defaults are never invented on the client."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="No data-control settings are recorded for this tenant yet."
    denied-message="Only tenant members can view data controls; tenant administrators can change them."
    @retry="resource.reload()"
  >
    <el-card class="settings-card" shadow="never">
      <template #header>{{ t("Retention & export policy") }}</template>
      <el-form label-position="top" class="settings-form">
        <el-form-item :label="t('Retention period (days)')">
          <el-input-number v-model="draft.retention_days" :min="0" :max="3650" :step="1" />
          <p class="muted">
            {{ t("Defaults") }}: {{ defaults.retention_days }} · {{ t("Effective value") }}:
            {{ effective?.retention_days ?? "—" }}
          </p>
        </el-form-item>
        <el-form-item :label="t('Export format')">
          <el-select v-model="exportFormat" style="max-width: 240px">
            <el-option v-for="format in EXPORT_FORMATS" :key="format" :label="format" :value="format" />
          </el-select>
          <p class="muted">
            {{ t("Defaults") }}: {{ defaults.export_format }} · {{ t("Effective value") }}:
            {{ effective?.export_format ?? "—" }}
          </p>
        </el-form-item>
        <el-form-item :label="t('Allow data export')">
          <el-switch v-model="draft.allow_export" />
          <p class="muted">
            {{ t("Defaults") }}: {{ defaults.allow_export ? "yes" : "no" }} · {{ t("Effective value") }}:
            {{ effective?.allow_export ? "yes" : "no" }}
          </p>
        </el-form-item>
      </el-form>
      <p v-if="updatedAt" class="muted">{{ t("Last updated") }}: {{ updatedAt }}</p>
    </el-card>

    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="settings-card__title">
          <span>{{ t("Export jobs") }}</span>
          <el-button
            type="primary"
            :disabled="!draft.allow_export"
            :title="draft.allow_export ? '' : t('Allow data export')"
            @click="openExportDialog"
          >
            {{ t("Request export") }}
          </el-button>
        </div>
      </template>
      <el-table v-if="jobs.length > 0" :data="jobs" size="small">
        <el-table-column prop="id" label="Job" width="220" />
        <el-table-column label="Status">
          <template #default="{ row }">
            <el-tag :type="exportStatusMeta(row.status).tagType" effect="plain">
              {{ t(exportStatusMeta(row.status).label) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('Row count')">
          <template #default="{ row }">{{ row.row_count ?? "—" }}</template>
        </el-table-column>
        <el-table-column prop="created_at" label="Created" />
        <el-table-column label="Download">
          <template #default="{ row }">
            <a v-if="canDownloadExport(row)" :href="row.download_url" :download="row.id">
              {{ t("Download") }}
            </a>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="Actions" width="160">
          <template #default="{ row }">
            <el-button
              v-if="row.status !== 'expired'"
              link
              type="warning"
              :loading="expiringJobId === row.id"
              @click="expireJob(row)"
            >
              {{ t("Expire export") }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else :description="t('No export jobs yet')" :image-size="64" />
    </el-card>

    <el-alert
      v-if="saveError"
      class="settings-inline-error"
      type="error"
      show-icon
      :closable="true"
      :title="saveError"
      @close="saveError = ''"
    />

    <template #footer>
      <el-button type="primary" :loading="saveState === 'saving'" @click="save">{{ t("Save") }}</el-button>
      <el-tag v-if="saveState === 'saved'" type="success" effect="plain">Saved</el-tag>
    </template>

    <el-dialog v-model="exportDialogVisible" :title="t('Confirm export')" width="520px">
      <el-alert type="warning" show-icon :closable="false" :title="t('Export scope')" :description="exportScopeWarning(draft.retention_days)" />
      <p class="muted">{{ t("Export format") }}: {{ exportFormat }}</p>
      <el-checkbox v-model="exportConfirmed">
        {{ t("I understand the export scope, retention period and that this action is irreversible.") }}
      </el-checkbox>
      <p class="muted">Idempotency key: {{ exportKey }}</p>
      <el-alert v-if="exportError" type="error" show-icon :closable="false" :title="exportError" />
      <template #footer>
        <el-button @click="exportDialogVisible = false">{{ t("Cancel") }}</el-button>
        <el-button
          type="primary"
          :disabled="!exportGateState.ok"
          :loading="exportSubmitting"
          @click="submitExport"
        >
          {{ t("Request export") }}
        </el-button>
      </template>
    </el-dialog>
  </SettingsPageShell>
</template>
