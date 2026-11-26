<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  ApiError,
  createCloudVm,
  cloudVmAction,
  deleteCloudVm,
  getCloudVmProviders,
  getCloudVms,
  type CloudVm,
  type CloudVmProvider,
} from "../services/api";
import { activeProjectId, activeTenantId, loadWorkspace } from "../state/workspace";
import { t, tf } from "../i18n";

const providers = ref<CloudVmProvider[]>([]);
const vms = ref<CloudVm[]>([]);
const warnings = ref<string[]>([]);
const loading = ref(false);
const busy = ref<string | null>(null);
const message = ref("");
const error = ref("");
const form = ref({
  provider: "",
  name: "",
  flavor_ref: "",
  image_ref: "",
  networks: "network-1",
});
const hasScope = computed(() => Boolean(activeTenantId.value && activeProjectId.value));
const canCreate = computed(
  () =>
    hasScope.value &&
    providers.value.length > 0 &&
    form.value.provider !== "" &&
    form.value.name.trim() !== "" &&
    form.value.flavor_ref.trim() !== "" &&
    form.value.image_ref.trim() !== "" &&
    form.value.networks.trim() !== "",
);

function explain(cause: unknown): string {
  if (cause instanceof ApiError) return cause.message;
  return cause instanceof Error ? cause.message : String(cause);
}

async function loadProviders(): Promise<void> {
  if (!hasScope.value) {
    providers.value = [];
    form.value.provider = "";
    return;
  }
  try {
    const response = await getCloudVmProviders(activeTenantId.value, activeProjectId.value);
    providers.value = response.results;
    if (!providers.value.some((item) => item.key === form.value.provider)) {
      form.value.provider = providers.value[0]?.key ?? "";
    }
  } catch (cause) {
    error.value = explain(cause);
  }
}

async function loadVms(refresh = false): Promise<void> {
  if (!hasScope.value) {
    vms.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const response = await getCloudVms(activeTenantId.value, activeProjectId.value, refresh);
    vms.value = response.results;
    warnings.value = response.warnings ?? [];
  } catch (cause) {
    error.value = explain(cause);
  } finally {
    loading.value = false;
  }
}

async function refreshAll(): Promise<void> {
  await loadProviders();
  await loadVms(true);
}

async function submit(): Promise<void> {
  if (!canCreate.value) return;
  busy.value = "create";
  error.value = "";
  message.value = "";
  const networks = form.value.networks
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  try {
    const created = await createCloudVm(activeTenantId.value, activeProjectId.value, {
      provider: form.value.provider,
      name: form.value.name.trim(),
      flavor_ref: form.value.flavor_ref.trim(),
      image_ref: form.value.image_ref.trim(),
      network_ids: networks,
    });
    message.value = tf("Cloud VM {0} requested with status {1}.", created.name, created.status);
    form.value.name = "";
    await loadVms(true);
  } catch (cause) {
    error.value = explain(cause);
  } finally {
    busy.value = null;
  }
}

async function act(vm: CloudVm, action: string): Promise<void> {
  busy.value = `${vm.id}:${action}`;
  error.value = "";
  message.value = "";
  try {
    const updated = await cloudVmAction(vm.id, action);
    message.value = tf("Cloud VM {0} -> {1}.", updated.name, updated.status);
    await loadVms(true);
  } catch (cause) {
    error.value = explain(cause);
  } finally {
    busy.value = null;
  }
}

async function remove(vm: CloudVm): Promise<void> {
  busy.value = `${vm.id}:delete`;
  error.value = "";
  message.value = "";
  try {
    const updated = await deleteCloudVm(vm.id);
    message.value = tf("Cloud VM {0} -> {1}.", updated.name, updated.status);
    await loadVms(true);
  } catch (cause) {
    error.value = explain(cause);
  } finally {
    busy.value = null;
  }
}

onMounted(async () => {
  await loadWorkspace();
  await refreshAll();
});

watch([activeTenantId, activeProjectId], () => void refreshAll());
</script>

<template>
  <section class="cloud-vms">
    <header class="cloud-header">
      <div>
        <p class="eyebrow">{{ t("Infrastructure") }}</p>
        <h1>{{ t("Cloud VMs") }}</h1>
        <p class="subtitle">
          {{ t("Create and operate virtual machines on operator-configured OpenStack providers.") }}
        </p>
      </div>
      <el-button :loading="loading" @click="refreshAll">{{ t("Refresh") }}</el-button>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-alert v-if="message" :title="message" type="success" show-icon :closable="false" />
    <el-alert
      v-for="warning in warnings"
      :key="warning"
      :title="warning"
      type="warning"
      show-icon
      :closable="false"
    />

    <section class="card">
      <div class="card-head">
        <h2>{{ t("Create a VM") }}</h2>
        <span v-if="!providers.length" class="hint">
          {{ t("No cloud provider is configured. Set CLOUD_VM_PROVIDERS in the deployment environment.") }}
        </span>
      </div>
      <div class="form-grid">
        <label>
          <span>{{ t("Provider") }}</span>
          <el-select v-model="form.provider" :disabled="!providers.length">
            <el-option
              v-for="provider in providers"
              :key="provider.key"
              :label="`${provider.display_name} · ${provider.region || provider.key}`"
              :value="provider.key"
            />
          </el-select>
        </label>
        <label>
          <span>{{ t("Name") }}</span>
          <el-input v-model="form.name" placeholder="worker-a" maxlength="63" />
        </label>
        <label>
          <span>{{ t("Flavor ref") }}</span>
          <el-input v-model="form.flavor_ref" placeholder="flavor-id-or-name" />
        </label>
        <label>
          <span>{{ t("Image ref") }}</span>
          <el-input v-model="form.image_ref" placeholder="image-id-or-name" />
        </label>
        <label class="wide">
          <span>{{ t("Networks") }}</span>
          <el-input v-model="form.networks" placeholder="network-id, network-id" />
          <small>{{ t("Comma-separated provider network ids; at least one is required.") }}</small>
        </label>
      </div>
      <div class="actions">
        <el-button type="primary" :disabled="!canCreate" :loading="busy === 'create'" @click="submit">
          {{ t("Create VM") }}
        </el-button>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2>{{ tf("{0} virtual machines", vms.length) }}</h2>
      </div>
      <div v-if="!vms.length" class="empty">
        {{ hasScope ? t("No virtual machines in this project yet.") : t("Select a tenant and project first.") }}
      </div>
      <div v-else class="vm-table">
        <article v-for="vm in vms" :key="vm.id" class="vm-row">
          <div class="vm-main">
            <div class="vm-title">
              <strong>{{ vm.name }}</strong>
              <span class="status" :data-status="vm.status.toLowerCase()">{{ vm.status }}</span>
            </div>
            <div class="vm-meta">
              <span>{{ vm.provider_key }}</span>
              <span>{{ vm.ip_address || t("No address yet") }}</span>
              <span>{{ vm.server_id }}</span>
            </div>
            <div class="vm-meta">
              <span>{{ t("Flavor") }}: {{ vm.flavor_ref }}</span>
              <span>{{ t("Image") }}: {{ vm.image_ref }}</span>
              <span>{{ t("Networks") }}: {{ vm.network_ids.join(", ") }}</span>
            </div>
          </div>
          <div class="vm-actions">
            <el-button
              size="small"
              :loading="busy === `${vm.id}:start`"
              @click="act(vm, 'start')"
            >{{ t("Start") }}</el-button>
            <el-button
              size="small"
              :loading="busy === `${vm.id}:stop`"
              @click="act(vm, 'stop')"
            >{{ t("Stop") }}</el-button>
            <el-button
              size="small"
              :loading="busy === `${vm.id}:reboot`"
              @click="act(vm, 'reboot')"
            >{{ t("Reboot") }}</el-button>
            <el-button
              size="small"
              type="danger"
              plain
              :loading="busy === `${vm.id}:delete`"
              @click="remove(vm)"
            >{{ t("Delete") }}</el-button>
          </div>
        </article>
      </div>
    </section>
  </section>
</template>

<style scoped>
.cloud-vms {
  display: grid;
  gap: 18px;
  padding: 24px clamp(16px, 3vw, 40px) 48px;
}
.cloud-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.eyebrow {
  margin: 0 0 6px;
  color: var(--muted, #6b7280);
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
h1 {
  margin: 0;
  font-size: clamp(24px, 3vw, 32px);
}
.subtitle {
  margin: 8px 0 0;
  max-width: 720px;
  color: var(--muted, #6b7280);
}
.card {
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 16px;
  background: var(--surface, #fff);
  padding: 18px;
  display: grid;
  gap: 16px;
}
.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.card-head h2 {
  margin: 0;
  font-size: 18px;
}
.hint,
.empty,
small {
  color: var(--muted, #6b7280);
  font-size: 13px;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 14px;
}
.form-grid label {
  display: grid;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}
.form-grid label > span {
  color: var(--muted, #6b7280);
}
.form-grid .wide {
  grid-column: 1 / -1;
}
.actions {
  display: flex;
  justify-content: flex-end;
}
.vm-table {
  display: grid;
  gap: 10px;
}
.vm-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding: 14px;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 12px;
  flex-wrap: wrap;
}
.vm-main {
  display: grid;
  gap: 8px;
  min-width: min(100%, 320px);
}
.vm-title {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.status {
  border-radius: 999px;
  background: #eef2ff;
  color: #4338ca;
  padding: 2px 10px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
}
.status[data-status="active"] {
  background: #ecfdf5;
  color: #047857;
}
.status[data-status="error"],
.status[data-status="unknown"] {
  background: #fef2f2;
  color: #b91c1c;
}
.vm-meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  color: var(--muted, #6b7280);
  font-size: 12px;
}
.vm-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
@media (max-width: 720px) {
  .cloud-header {
    flex-direction: column;
  }
  .vm-actions {
    width: 100%;
  }
}
</style>
