<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { fetchShortcutPreferences, fetchShortcutRegistry, saveShortcutPreferences } from "./api";
import {
  buildBindings,
  describeError,
  findConflicts,
  normalizeCombination,
  pageLabels,
  validateCombination,
  type ShortcutBindings,
  type ShortcutRegistryEntry,
} from "./logic";

const registry = ref<ShortcutRegistryEntry[]>([]);
const bindings = ref<ShortcutBindings>({});
const defaults = ref<ShortcutBindings>({});
const saveState = ref<"idle" | "saving" | "saved" | "error">("idle");
const saveError = ref("");

const conflicts = computed(() => findConflicts(bindings.value));
const conflictingIds = computed(() => new Set(conflicts.value.map((entry) => entry.id)));

const resource = useSettingsResource(async () => {
  const registryPayload = await fetchShortcutRegistry();
  registry.value = registryPayload.results;
  const preferences = await fetchShortcutPreferences();
  defaults.value = buildBindings(registry.value, null);
  bindings.value = buildBindings(registry.value, preferences.effective);
  return { registry: registry.value, bindings: bindings.value };
});

function update(id: string, raw: string): void {
  const normalized = normalizeCombination(raw);
  bindings.value = { ...bindings.value, [id]: normalized || raw };
}

function reset(id: string): void {
  bindings.value = { ...bindings.value, [id]: defaults.value[id] };
}

async function save(): Promise<void> {
  saveError.value = "";
  for (const entry of registry.value) {
    const validation = validateCombination(bindings.value[entry.id] ?? "");
    if (!validation.ok) {
      saveState.value = "error";
      saveError.value = `${t("Shortcut reference")} "${entry.description}": ${describeError(validation.code)}`;
      return;
    }
  }
  if (conflicts.value.length > 0) {
    saveState.value = "error";
    saveError.value = `${describeError("SHORTCUT_CONFLICT")} ${conflicts.value
      .map((entry) => `${entry.id} ↔ ${entry.conflictsWith.join(", ")}`)
      .join("; ")}`;
    return;
  }
  saveState.value = "saving";
  try {
    const echoed = await saveShortcutPreferences(bindings.value);
    bindings.value = buildBindings(registry.value, echoed.effective);
    saveState.value = "saved";
  } catch (cause) {
    saveState.value = "error";
    saveError.value = cause instanceof ApiError ? describeError(cause.code, cause.details) : "Shortcuts could not be saved.";
  }
}

onMounted(() => void resource.reload());
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Keyboard Shortcuts"
    description="The shortcut registry is authoritative and comes from the server; your overrides are stored per user."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="The shortcut registry is empty for this deployment."
    denied-message="Sign in to manage your keyboard shortcuts."
    @retry="resource.reload()"
  >
    <el-alert
      v-if="resource.errorCode.value === 'SHORTCUT_REGISTRY_UNAVAILABLE'"
      type="error"
      show-icon
      :closable="false"
      :title="t('Shortcut registry unavailable')"
    />

    <el-table :data="registry" size="small">
      <el-table-column prop="description" :label="t('Shortcut reference')" />
      <el-table-column prop="default_combination" :label="t('Default combination')" width="180" />
      <el-table-column :label="t('Your combination')" width="220">
        <template #default="{ row }">
          <el-input
            :model-value="bindings[row.id]"
            :class="{ 'is-conflict': conflictingIds.has(row.id) }"
            @update:model-value="update(row.id, $event)"
          />
        </template>
      </el-table-column>
      <el-table-column :label="t('Reset to default')" width="150">
        <template #default="{ row }">
          <el-button text @click="reset(row.id)">{{ t("Reset to default") }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-alert
      v-if="conflicts.length > 0"
      type="warning"
      show-icon
      :closable="false"
      :title="t('Conflicting shortcut')"
      :description="conflicts.map((entry) => `${entry.combination}: ${entry.id} ↔ ${entry.conflictsWith.join(', ')}`).join('; ')"
    />

    <template #footer>
      <el-button type="primary" :loading="saveState === 'saving'" @click="save">{{ t("Save") }}</el-button>
      <el-tag v-if="saveState === 'saved'" type="success" effect="plain">Saved</el-tag>
      <el-alert v-if="saveError" type="error" show-icon :closable="false" :title="saveError" />
      <p class="muted">{{ t("Shortcuts are stored per user") }}</p>
    </template>
  </SettingsPageShell>
</template>
