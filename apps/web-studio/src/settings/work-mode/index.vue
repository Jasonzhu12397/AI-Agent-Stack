<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { activeProjectId, activeTenantId, ensureTenantContext, tenantContextError } from "../shared/tenantContext";
import { fetchWorkMode, saveWorkMode } from "./api";
import {
  MAX_CONCURRENCY_LIMIT,
  WORK_MODE_DEFAULTS,
  describeError,
  normalizeWorkMode,
  pageLabels,
  sourceLabel,
  validateWorkMode,
  type WorkModeValues,
} from "./logic";

const scope = ref<"tenant" | "project">("tenant");
const draft = ref<WorkModeValues>({ ...WORK_MODE_DEFAULTS });
const sources = ref<Record<string, string>>({});
const saveState = ref<"idle" | "saving" | "saved" | "error">("idle");
const saveError = ref("");

function projectScope(): string | null {
  return scope.value === "project" && activeProjectId.value ? activeProjectId.value : null;
}

const resource = useSettingsResource(async () => {
  await ensureTenantContext();
  if (!activeTenantId.value) {
    throw new ApiError(400, "TENANT_CONTEXT_UNAVAILABLE", tenantContextError.value || "No tenant is available.");
  }
  const payload = await fetchWorkMode(activeTenantId.value, projectScope());
  draft.value = normalizeWorkMode(payload.effective);
  sources.value = payload.sources ?? {};
  return payload;
});

async function switchScope(next: "tenant" | "project"): Promise<void> {
  scope.value = next;
  await resource.reload();
}

async function save(): Promise<void> {
  if (!activeTenantId.value) return;
  const validation = validateWorkMode(draft.value);
  if (!validation.ok) {
    saveState.value = "error";
    saveError.value = describeError(Object.values(validation.errors)[0]);
    return;
  }
  saveState.value = "saving";
  saveError.value = "";
  try {
    const echoed = await saveWorkMode(activeTenantId.value, projectScope(), draft.value);
    draft.value = normalizeWorkMode(echoed.effective);
    sources.value = echoed.sources ?? {};
    saveState.value = "saved";
  } catch (cause) {
    saveState.value = "error";
    saveError.value = cause instanceof ApiError ? describeError(cause.code) : "Work mode could not be saved.";
  }
}

const scopeHint = computed(() => (scope.value === "project" && !activeProjectId.value ? "No project is selected." : ""));

onMounted(() => void resource.reload());
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Work Mode Settings"
    description="Execution preferences, confirmation behaviour and workspace defaults. Project values override tenant values; the source of every key is shown."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="No work-mode settings are stored yet; defaults apply."
    denied-message="Only project administrators can change project work-mode settings."
    @retry="resource.reload()"
  >
    <el-radio-group :model-value="scope" class="settings-scope" @change="switchScope($event as 'tenant' | 'project')">
      <el-radio-button value="tenant">{{ t("Tenant scope") }}</el-radio-button>
      <el-radio-button value="project">{{ t("Project scope") }}</el-radio-button>
    </el-radio-group>
    <p v-if="scopeHint" class="muted">{{ t(scopeHint) }}</p>

    <el-form label-position="top" class="settings-form">
      <el-form-item :label="t('Default execution mode')">
        <el-radio-group v-model="draft.default_execution_mode">
          <el-radio-button value="review">Review before running</el-radio-button>
          <el-radio-button value="auto">Run automatically</el-radio-button>
        </el-radio-group>
        <p class="muted">{{ t("Scope") }}: {{ t(sourceLabel(sources.default_execution_mode ?? "default")) }}</p>
      </el-form-item>
      <el-form-item :label="t('Require confirmation')">
        <el-switch v-model="draft.require_confirmation" />
      </el-form-item>
      <el-form-item :label="t('Auto continue')">
        <el-switch v-model="draft.auto_continue" />
      </el-form-item>
      <el-form-item :label="t('Max concurrency')">
        <el-input-number v-model="draft.max_concurrency" :min="1" :max="MAX_CONCURRENCY_LIMIT" />
        <p class="muted">{{ t("Scope") }}: {{ t(sourceLabel(sources.max_concurrency ?? "default")) }}</p>
      </el-form-item>
      <el-form-item :label="t('Workspace default branch')">
        <el-input v-model="draft.workspace_default_branch" placeholder="main" />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button type="primary" :loading="saveState === 'saving'" @click="save">{{ t("Save") }}</el-button>
      <el-tag v-if="saveState === 'saved'" type="success" effect="plain">Saved</el-tag>
      <el-alert v-if="saveError" type="error" show-icon :closable="false" :title="saveError" />
    </template>
  </SettingsPageShell>
</template>
