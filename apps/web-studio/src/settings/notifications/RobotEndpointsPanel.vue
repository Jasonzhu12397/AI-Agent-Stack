<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import {
  ROBOT_CHANNELS,
  createEndpoint,
  deleteEndpoint,
  listEndpoints,
  testEndpoint,
  type NotificationEndpoint,
} from "./endpoints";

const props = defineProps<{ tenantId: string }>();

const rows = ref<NotificationEndpoint[]>([]);
const loading = ref(false);
const error = ref("");
const info = ref("");
const dialogOpen = ref(false);
const form = ref({ channel: "dingtalk", name: "", webhook_url: "", secret: "" });
const busyId = ref("");

const selectedChannel = computed(
  () => ROBOT_CHANNELS.find((item) => item.value === form.value.channel) ?? ROBOT_CHANNELS[0],
);
const secretRequired = computed(() => form.value.channel === "dingtalk");

async function refresh(): Promise<void> {
  if (!props.tenantId) return;
  loading.value = true;
  error.value = "";
  try {
    rows.value = await listEndpoints(props.tenantId);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load notification robots.";
  } finally {
    loading.value = false;
  }
}

function channelLabel(channel: string): string {
  return t(ROBOT_CHANNELS.find((item) => item.value === channel)?.label ?? channel);
}

function openDialog(): void {
  form.value = { channel: "dingtalk", name: "", webhook_url: "", secret: "" };
  error.value = "";
  dialogOpen.value = true;
}

async function save(): Promise<void> {
  if (!props.tenantId) return;
  if (!form.value.name.trim() || !form.value.webhook_url.trim()) {
    error.value = "Name and webhook URL are required.";
    return;
  }
  if (secretRequired.value && !form.value.secret.trim()) {
    error.value = "DingTalk robot signing secret is required when 加签 is enabled.";
    return;
  }
  loading.value = true;
  try {
    await createEndpoint(props.tenantId, {
      channel: form.value.channel,
      name: form.value.name.trim(),
      webhook_url: form.value.webhook_url.trim(),
      secret: form.value.secret.trim(),
    });
    dialogOpen.value = false;
    info.value = "Robot added.";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "Unable to add the robot.";
  } finally {
    loading.value = false;
  }
}

async function sendTest(row: NotificationEndpoint): Promise<void> {
  if (!props.tenantId) return;
  busyId.value = row.id;
  error.value = "";
  info.value = "";
  try {
    const result = await testEndpoint(props.tenantId, row.id);
    info.value = result.ok ? "Test message delivered." : `Delivery refused: ${result.message}`;
    await refresh();
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "Test delivery failed.";
    await refresh();
  } finally {
    busyId.value = "";
  }
}

async function remove(row: NotificationEndpoint): Promise<void> {
  if (!props.tenantId) return;
  busyId.value = row.id;
  try {
    await deleteEndpoint(props.tenantId, row.id);
    await refresh();
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "Unable to remove the robot.";
  } finally {
    busyId.value = "";
  }
}

watch(() => props.tenantId, () => void refresh());
onMounted(() => void refresh());
</script>

<template>
  <el-card class="settings-card" shadow="never" data-notification-robots>
    <template #header>
      <div class="robot-head">
        <span>{{ t("IM group robots") }}</span>
        <el-button size="small" type="primary" plain @click="openDialog">
          {{ t("Add robot") }}
        </el-button>
      </div>
    </template>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="info" :title="info" type="success" show-icon :closable="true" @close="info = ''" />
    <p class="robot-hint">
      钉钉、企业微信群、飞书群机器人各一条 Webhook；地址与加签密钥加密存储，只显示是否已送达。
    </p>
    <el-table v-loading="loading" :data="rows" size="small" :border="false">
      <el-table-column :label="t('Channel')" width="180">
        <template #default="{ row }">{{ channelLabel(row.channel) }}</template>
      </el-table-column>
      <el-table-column prop="name" :label="t('Name')" min-width="160" />
      <el-table-column :label="t('Last delivery')" min-width="200">
        <template #default="{ row }">
          <span v-if="row.last_error" class="robot-error">{{ row.last_error }}</span>
          <span v-else-if="row.last_sent_at" class="muted">{{ row.last_sent_at }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('Actions')" width="220">
        <template #default="{ row }">
          <el-button size="small" :loading="busyId === row.id" @click="sendTest(row)">
            {{ t("Send test") }}
          </el-button>
          <el-button size="small" text @click="remove(row)">{{ t("Remove") }}</el-button>
        </template>
      </el-table-column>
    </el-table>
    <p v-if="!rows.length && !loading" class="muted">还没有群机器人，先加一个钉钉或企微群。</p>

    <el-dialog v-model="dialogOpen" :title="t('Add robot')" width="560px">
      <el-form label-position="top">
        <el-form-item :label="t('Channel')">
          <el-select v-model="form.channel" style="width: 100%">
            <el-option
              v-for="channel in ROBOT_CHANNELS"
              :key="channel.value"
              :label="t(channel.label)"
              :value="channel.value"
            />
          </el-select>
        </el-form-item>
        <p class="robot-note">{{ selectedChannel.note }}</p>
        <el-form-item :label="t('Name')">
          <el-input v-model="form.name" placeholder="Ops alerts" />
        </el-form-item>
        <el-form-item :label="t('Webhook URL')">
          <el-input v-model="form.webhook_url" type="textarea" :rows="3" placeholder="https://…" />
        </el-form-item>
        <el-form-item :label="secretRequired ? t('Signing secret (required)') : t('Signing secret (optional)')">
          <el-input v-model="form.secret" autocomplete="off" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ t("Cancel") }}</el-button>
        <el-button type="primary" :loading="loading" @click="save">{{ t("Save") }}</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.robot-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.robot-hint,
.robot-note {
  margin: 0 0 10px;
  font-size: 12px;
  color: #5f6b78;
}
.robot-error {
  color: #b3261e;
  font-size: 12px;
}
</style>
