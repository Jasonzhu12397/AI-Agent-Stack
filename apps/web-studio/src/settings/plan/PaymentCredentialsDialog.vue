<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { t } from "../../i18n";
import { tenantPath } from "../../services/http";
import { request } from "../../services/http";

type ChannelCredentials = {
  channel: string;
  required: string[];
  slots: string[];
  filled: string[];
  missing: string[];
  configured: boolean;
};

const props = defineProps<{ tenantId: string; channel: string; open: boolean }>();
const emit = defineEmits<{ (event: "update:open", value: boolean): void; (event: "saved"): void }>();

const loading = ref(false);
const saving = ref(false);
const error = ref("");
const report = ref<ChannelCredentials | null>(null);
const values = ref<Record<string, string>>({});

const slotOrder = computed(() => report.value?.slots ?? []);
const visible = computed({
  get: () => props.open,
  set: (value: boolean) => emit("update:open", value),
});

async function load(): Promise<void> {
  if (!props.channel || !props.tenantId) return;
  loading.value = true;
  error.value = "";
  try {
    report.value = await request<ChannelCredentials>(
      tenantPath(props.tenantId, `/billing/payments/credentials/${props.channel}/`),
    );
    values.value = {};
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to read the channel.";
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.open, props.channel, props.tenantId],
  () => {
    if (props.open) void load();
  },
);

async function save(): Promise<void> {
  const payload = Object.fromEntries(
    Object.entries(values.value).filter(([, value]) => value.trim().length > 0),
  );
  if (!Object.keys(payload).length) {
    error.value = "Fill at least one field before saving.";
    return;
  }
  saving.value = true;
  error.value = "";
  try {
    report.value = await request<ChannelCredentials>(
      tenantPath(props.tenantId, `/billing/payments/credentials/${props.channel}/`),
      { method: "PUT", body: JSON.stringify({ values: payload }) },
    );
    values.value = {};
    emit("saved");
    emit("update:open", false);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to save credentials.";
  } finally {
    saving.value = false;
  }
}

function slotLabel(slot: string): string {
  return slot.replace(/_/g, " ");
}

function isFilled(slot: string): boolean {
  return Boolean(report.value?.filled.includes(slot));
}
</script>

<template>
  <el-dialog v-model="visible" :title="`${t('Payment credentials')} · ${channel}`" width="640px">
    <div v-loading="loading">
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
      <p class="dialog-hint">
        Values are sealed with the deployment key and never returned. Environment
        variables still win when both are set, so a container-injected merchant key
        stays authoritative.
      </p>
      <el-form label-position="top">
        <el-form-item v-for="slot in slotOrder" :key="slot" :label="slotLabel(slot)">
          <el-input
            v-model="values[slot]"
            :type="slot === 'private_key' || slot === 'platform_cert' ? 'textarea' : 'text'"
            :rows="4"
            :placeholder="isFilled(slot) ? 'Saved — leave blank to keep it' : 'Paste the merchant value'"
            autocomplete="off"
          />
        </el-form-item>
      </el-form>
      <p class="dialog-hint" data-channel-state>
        {{ report?.configured ? "All required slots resolve; this channel can sell." : `Missing: ${(report?.missing ?? []).join(", ") || "—"}` }}
      </p>
    </div>
    <template #footer>
      <el-button @click="visible = false">{{ t("Cancel") }}</el-button>
      <el-button type="primary" :loading="saving" @click="save">{{ t("Save") }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.dialog-hint {
  margin: 4px 0 12px;
  font-size: 12px;
  color: #5f6b78;
  line-height: 1.5;
}
</style>
