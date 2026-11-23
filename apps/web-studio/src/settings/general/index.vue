<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { request, ApiError } from "../../services/http";
import { useSettingsResource } from "../shared/useSettingsResource";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { activeTenantId, activeProjectId } from "../shared/tenantContext";
import { scopeLabel } from "../shared/scope";
import {
  generalScopePath,
  foldEffective,
  validateGeneralValues,
  fieldErrorsFromError,
  stateFromError,
  errorMessageKey,
  type GeneralEnvelope,
  type GeneralScope,
} from "./logic";

const scope = ref<GeneralScope>("tenant");

const general = useSettingsResource<GeneralEnvelope>(
  loadEnvelope,
  (data) => Object.keys(data.defaults).length === 0,
);

const form = ref<Record<string, unknown>>({});
const saving = ref(false);
const savedNotice = ref(false);
const saveError = ref("");
const saveCode = ref("");
const fieldErrors = ref<Record<string, string[]>>({});

async function loadEnvelope(): Promise<GeneralEnvelope> {
  if (!activeTenantId.value) {
    throw new ApiError(404, "TENANT_MISSING", "No tenant selected.");
  }
  const projectId = scope.value === "project" ? activeProjectId.value : "";
  return request<GeneralEnvelope>(generalScopePath(scope.value, activeTenantId.value, projectId));
}

const defaults = computed(() => general.data.value?.defaults ?? {});

const folded = computed(() => {
  if (!general.data.value) return null;
  const layers =
    scope.value === "project"
      ? { project: general.data.value.values }
      : { tenant: general.data.value.values };
  return foldEffective(general.data.value.defaults, layers);
});

const sources = computed(() => folded.value?.sources ?? {});
const updatedAt = computed(() => general.data.value?.updated_at ?? "");
const settingKeys = computed(() => Object.keys(defaults.value));

async function reload(): Promise<void> {
  await general.reload();
  if (general.data.value) {
    form.value = { ...general.data.value.effective, ...general.data.value.values };
  }
}

onMounted(() => {
  void reload();
});

watch(scope, () => {
  savedNotice.value = false;
  saveError.value = "";
  fieldErrors.value = {};
  void reload();
});

function inputType(key: string): "boolean" | "number" | "text" {
  const value = defaults.value[key];
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "number") return "number";
  return "text";
}

function firstError(key: string): string {
  const reasons = fieldErrors.value[key];
  return reasons?.[0] ? errorMessageKey(reasons[0]) : "";
}

async function save(): Promise<void> {
  saveError.value = "";
  saveCode.value = "";
  fieldErrors.value = {};
  savedNotice.value = false;

  const errors = validateGeneralValues(form.value, defaults.value);
  if (Object.keys(errors).length > 0) {
    fieldErrors.value = errors;
    return;
  }

  saving.value = true;
  try {
    const projectId = scope.value === "project" ? activeProjectId.value : "";
    const updated = await request<GeneralEnvelope>(
      generalScopePath(scope.value, activeTenantId.value, projectId),
      { method: "PATCH", body: JSON.stringify({ values: form.value }) },
    );
    general.data.value = updated;
    form.value = { ...updated.values };
    savedNotice.value = true;
  } catch (cause) {
    const mapped = stateFromError(cause);
    if (mapped.kind === "denied") general.state.value = "denied";
    if (cause instanceof ApiError) {
      saveCode.value = cause.code;
      saveError.value = cause.message || errorMessageKey(cause.code);
      fieldErrors.value = fieldErrorsFromError(cause);
    } else {
      saveError.value = cause instanceof Error ? cause.message : "Settings could not be saved.";
    }
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <SettingsPageShell
    title="General"
    description="General settings for the selected scope."
    :state="general.state.value"
    :error-code="general.errorCode.value"
    :error-message="general.errorMessage.value"
    empty-message="No settings available for this scope."
    @retry="reload"
  >
    <div class="general-toolbar">
      <el-radio-group v-model="scope" aria-label="Scope">
        <el-radio-button value="tenant">Tenant</el-radio-button>
        <el-radio-button value="project">Project</el-radio-button>
      </el-radio-group>
      <span v-if="updatedAt" class="muted">Updated: {{ updatedAt }}</span>
    </div>

    <el-alert
      v-if="saveError"
      class="settings-form-alert"
      type="error"
      show-icon
      :closable="false"
      :title="saveCode ? `${saveCode}: ${saveError}` : saveError"
    />

    <div v-if="settingKeys.length > 0" class="general-grid">
      <div v-for="key in settingKeys" :key="key" class="settings-card general-row">
        <div class="general-row__heading">
          <span class="settings-list__title">{{ key }}</span>
          <span class="muted">{{ scopeLabel(sources[key] ?? "default") }}</span>
        </div>
        <el-form label-position="top" class="inline-form">
          <el-form-item :label="`Effective value: ${folded?.values[key] ?? defaults[key]}`" :error="firstError(key)">
            <el-switch
              v-if="inputType(key) === 'boolean'"
              v-model="form[key]"
            />
            <el-input-number
              v-else-if="inputType(key) === 'number'"
              v-model="form[key]"
              class="settings-form-control"
            />
            <el-input v-else v-model="form[key]" />
          </el-form-item>
        </el-form>
        <p class="muted general-row__default">Default: {{ defaults[key] }}</p>
      </div>
    </div>

    <div class="settings-card__actions">
      <el-button type="primary" :loading="saving" @click="save">Save changes</el-button>
      <el-tag v-if="savedNotice" type="success" effect="plain">Saved</el-tag>
    </div>
  </SettingsPageShell>
</template>
