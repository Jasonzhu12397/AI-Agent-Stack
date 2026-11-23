<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { fetchAppearance, saveAppearance } from "./api";
import {
  APPEARANCE_DEFAULTS,
  APPEARANCE_STORAGE_KEY,
  applyAppearance,
  describeError,
  normalizeAppearance,
  pageLabels,
  readCachedAppearance,
  resolveTheme,
  validateAppearance,
  type AppearanceSettings,
} from "./logic";

const draft = ref<AppearanceSettings>({ ...APPEARANCE_DEFAULTS });
const saveState = ref<"idle" | "saving" | "saved" | "error">("idle");
const saveError = ref("");

function browserStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function persistLocally(settings: AppearanceSettings): void {
  const storage = browserStorage();
  if (!storage) return;
  try {
    storage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify(settings));
  } catch {
    /* quota or private mode: the server copy remains authoritative */
  }
}

const resource = useSettingsResource(
  async () => {
    draft.value = readCachedAppearance(browserStorage());
    applyAppearance(draft.value, document.documentElement);
    const payload = await fetchAppearance();
    draft.value = normalizeAppearance(payload.values);
    applyAppearance(draft.value, document.documentElement);
    persistLocally(draft.value);
    return payload;
  },
  () => false,
);

watch(draft, (value) => {
  applyAppearance(value, document.documentElement);
}, { deep: true });

async function save(): Promise<void> {
  const validation = validateAppearance(draft.value);
  if (!validation.ok) {
    saveState.value = "error";
    saveError.value = describeError(Object.values(validation.errors)[0]);
    return;
  }
  saveState.value = "saving";
  saveError.value = "";
  try {
    const payload = await saveAppearance(draft.value);
    draft.value = normalizeAppearance(payload.values);
    persistLocally(draft.value);
    saveState.value = "saved";
  } catch (cause) {
    saveState.value = "error";
    saveError.value = cause instanceof ApiError ? describeError(cause.code) : "Preferences could not be saved.";
  }
}

function previewSystemTheme(): void {
  draft.value = { ...draft.value, theme: resolveTheme(draft.value.theme, false) === "dark" ? "dark" : "light" };
}

onMounted(() => void resource.reload());
void previewSystemTheme;
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Appearance"
    description="Theme, density and display preferences. Preferences are stored per user; a missing local cache falls back to the server value and then to the defaults."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="No appearance preferences are stored yet; defaults are applied."
    denied-message="Sign in to change your appearance preferences."
    @retry="resource.reload()"
  >
    <el-form label-position="top" class="settings-form">
      <el-form-item :label="t('Theme')">
        <el-radio-group v-model="draft.theme" data-field="theme">
          <el-radio-button value="light">{{ t("Light") }}</el-radio-button>
          <el-radio-button value="dark">{{ t("Dark") }}</el-radio-button>
          <el-radio-button value="system">{{ t("System") }}</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item :label="t('Density')">
        <el-radio-group v-model="draft.density" data-field="density">
          <el-radio-button value="comfortable">{{ t("Comfortable") }}</el-radio-button>
          <el-radio-button value="compact">{{ t("Compact") }}</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item :label="t('Font scale')">
        <el-slider v-model="draft.font_scale" :min="0.875" :max="1.5" :step="0.125" show-input data-field="font_scale" />
      </el-form-item>
      <el-form-item :label="t('Reduce motion')">
        <el-switch v-model="draft.reduced_motion" data-field="reduced_motion" />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button type="primary" :loading="saveState === 'saving'" @click="save">{{ t("Save") }}</el-button>
      <el-tag v-if="saveState === 'saved'" type="success" effect="plain">Saved</el-tag>
      <el-alert v-if="saveError" type="error" show-icon :closable="false" :title="saveError" />
      <p class="muted">{{ t("Preferences are stored per user") }}</p>
    </template>
  </SettingsPageShell>
</template>
