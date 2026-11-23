<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { activeTenantId, ensureTenantContext, tenantContextError } from "../shared/tenantContext";
import { fetchDeliveryPreferences, fetchTenantNotifications, saveTenantNotifications } from "./api";
import {
  CATEGORIES,
  CHANNELS,
  describeError,
  digestLabel,
  foldDelivery,
  normalizeMatrix,
  pageLabels,
  toggleChannel,
  type NotificationMatrix,
} from "./logic";

const draft = ref<NotificationMatrix | null>(null);
const digest = ref("immediate");
const delivery = ref<Record<string, boolean>>({});
const saveState = ref<"idle" | "saving" | "saved" | "error">("idle");
const saveError = ref("");

const resource = useSettingsResource(async () => {
  await ensureTenantContext();
  if (!activeTenantId.value) {
    throw new ApiError(400, "TENANT_CONTEXT_UNAVAILABLE", tenantContextError.value || "No tenant is available.");
  }
  const [tenantSettings, deliverySettings] = await Promise.all([
    fetchTenantNotifications(activeTenantId.value),
    fetchDeliveryPreferences(),
  ]);
  draft.value = normalizeMatrix(tenantSettings.values?.matrix);
  digest.value = tenantSettings.values?.digest_frequency ?? "immediate";
  delivery.value = { ...(deliverySettings.effective ?? {}) };
  return tenantSettings;
});

const effective = computed(() =>
  draft.value ? foldDelivery(draft.value, delivery.value as Partial<Record<"in_app" | "email" | "webhook", boolean>>) : null,
);

function cellLabel(category: string): string {
  return category
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function cellValue(category: string, channel: string): boolean {
  if (!draft.value) return false;
  const row = draft.value[category as keyof NotificationMatrix];
  return Boolean(row?.[channel as keyof typeof row]);
}

function toggle(category: (typeof CATEGORIES)[number], channel: (typeof CHANNELS)[number]): void {
  if (!draft.value) return;
  draft.value = toggleChannel(draft.value, category, channel);
}

async function save(): Promise<void> {
  if (!draft.value || !activeTenantId.value) return;
  saveState.value = "saving";
  saveError.value = "";
  try {
    const echoed = await saveTenantNotifications(activeTenantId.value, {
      matrix: draft.value,
      digest_frequency: digest.value,
    });
    draft.value = normalizeMatrix(echoed.values?.matrix);
    digest.value = echoed.values?.digest_frequency ?? digest.value;
    saveState.value = "saved";
  } catch (cause) {
    saveState.value = "error";
    saveError.value = cause instanceof ApiError ? describeError(cause.code) : "Notification rules could not be saved.";
  }
}

onMounted(() => void resource.reload());
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Notification Settings"
    description="Event categories, delivery channels and digest frequency. Tenant rules define what can be delivered; your own preference can only narrow them."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="No notification rules are defined for this tenant yet."
    denied-message="Only tenant members can read notification rules; administrators can change them."
    @retry="resource.reload()"
  >
    <el-card v-if="draft" class="settings-card" shadow="never">
      <template #header>{{ t("Tenant notification rules") }}</template>
      <el-table :data="CATEGORIES.map((category) => ({ category }))" size="small" data-notification-matrix>
        <el-table-column label="Event categories">
          <template #default="{ row }">{{ t(cellLabel(row.category)) }}</template>
        </el-table-column>
        <el-table-column v-for="channel in CHANNELS" :key="channel" :label="t(channel === 'in_app' ? 'In-app' : channel === 'email' ? 'Email' : 'Webhook')">
          <template #default="{ row }">
            <el-checkbox
              :model-value="cellValue(row.category, channel)"
              @change="toggle(row.category as (typeof CATEGORIES)[number], channel as (typeof CHANNELS)[number])"
            />
          </template>
        </el-table-column>
      </el-table>

      <el-form label-position="top" class="settings-form">
        <el-form-item :label="t('Digest frequency')">
          <el-select v-model="digest" style="max-width: 240px">
            <el-option label="Immediate delivery" value="immediate" />
            <el-option label="Daily digest" value="daily" />
            <el-option label="Weekly digest" value="weekly" />
          </el-select>
          <p class="muted">{{ t(digestLabel(digest)) }}</p>
        </el-form-item>
      </el-form>

      <el-collapse>
        <el-collapse-item :title="t('User delivery preferences')" name="delivery">
          <p class="muted">
            Turning a channel off here applies to you only and narrows the tenant rules above.
          </p>
          <el-checkbox
            v-for="channel in CHANNELS"
            :key="channel"
            v-model="delivery[channel]"
            :label="t(channel === 'in_app' ? 'In-app' : channel === 'email' ? 'Email' : 'Webhook')"
          />
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <template #footer>
      <el-button type="primary" :loading="saveState === 'saving'" :disabled="!draft" @click="save">{{ t("Save") }}</el-button>
      <el-tag v-if="saveState === 'saved'" type="success" effect="plain">Saved</el-tag>
      <el-alert v-if="saveError" type="error" show-icon :closable="false" :title="saveError" />
      <p v-if="effective" class="muted">{{ t("Delivery channels") }}: {{ CHANNELS.join(", ") }}</p>
    </template>
  </SettingsPageShell>
</template>
