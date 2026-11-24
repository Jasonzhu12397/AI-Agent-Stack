<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  createAccountProvider,
  getModelProviders,
  getProviderCatalog,
  linkProviderAccount,
  unlinkProviderAccount,
  type ModelProvider,
  type ProviderCatalog,
} from "../services/api";

// A tenant that has not bought tokens can still let members drive OpenAI,
// Google, DeepSeek or Qwen through their own account on those platforms: the
// provider row is only a catalogue entry, the credential belongs to the
// member, and each call draws on the free per-member daily allowance.
const props = defineProps<{ tenantId: string }>();

const catalog = ref<ProviderCatalog | null>(null);
const providers = ref<ModelProvider[]>([]);
const loading = ref(false);
const error = ref("");
const info = ref("");

const dialogOpen = ref(false);
const dialogPreset = ref("");
const credential = ref("");
const accountLabel = ref("");
const saving = ref(false);

const platformRows = computed(() =>
  (catalog.value?.presets ?? []).map((preset) => {
    const provider = providers.value.find(
      (item) => item.preset_key === preset.key && item.auth_mode === "user_account",
    );
    return {
      preset,
      provider: provider ?? null,
      linked: provider?.account?.linked ?? false,
      remaining: provider?.account?.remaining_today ?? catalog.value?.free_daily_tokens ?? 0,
      limit: provider?.account?.free_daily_tokens ?? catalog.value?.free_daily_tokens ?? 0,
      accountLabel: provider?.account?.account_label ?? "",
    };
  }),
);

async function refresh(): Promise<void> {
  if (!props.tenantId) return;
  loading.value = true;
  error.value = "";
  try {
    const [nextCatalog, nextProviders] = await Promise.all([
      getProviderCatalog(props.tenantId),
      getModelProviders(props.tenantId),
    ]);
    catalog.value = nextCatalog;
    providers.value = nextProviders;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load platform accounts.";
  } finally {
    loading.value = false;
  }
}

function openDialog(presetKey: string): void {
  dialogPreset.value = presetKey;
  credential.value = "";
  accountLabel.value = "";
  dialogOpen.value = true;
}

async function connect(): Promise<void> {
  const presetKey = dialogPreset.value;
  if (!presetKey || !credential.value.trim()) return;
  saving.value = true;
  error.value = "";
  try {
    let provider = providers.value.find(
      (item) => item.preset_key === presetKey && item.auth_mode === "user_account",
    );
    if (!provider) {
      const label = catalog.value?.presets.find((item) => item.key === presetKey)?.label ?? presetKey;
      provider = await createAccountProvider(props.tenantId, presetKey, `${label} (my account)`);
      providers.value = [...providers.value, provider];
    }
    const account = await linkProviderAccount(
      props.tenantId,
      provider.id,
      credential.value.trim(),
      accountLabel.value.trim(),
    );
    providers.value = providers.value.map((item) =>
      item.id === provider?.id ? { ...item, account } : item,
    );
    info.value = "Account linked. Models on this platform now run on your own account and free allowance.";
    dialogOpen.value = false;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to link the account.";
  } finally {
    saving.value = false;
  }
}

async function disconnect(providerId: string): Promise<void> {
  error.value = "";
  try {
    const account = await unlinkProviderAccount(props.tenantId, providerId);
    providers.value = providers.value.map((item) =>
      item.id === providerId ? { ...item, account } : item,
    );
    info.value = "Account unlinked.";
    void account;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to unlink the account.";
  }
}

onMounted(refresh);
watch(() => props.tenantId, refresh);
defineExpose({ openPreset: openDialog });
</script>

<template>
  <section class="integration-panel platform-accounts">
    <div class="integration-panel-title">
      <h3>Platform accounts · free allowance</h3>
      <span>{{ platformRows.filter((row) => row.linked).length }} connected</span>
    </div>
    <p class="hint">
      Authorize your own OpenAI, Google, DeepSeek or Qwen account once. Its models then show up in
      Model Manager and in the Control Agent chat, and calls draw on the free per-member daily
      allowance instead of a purchased tenant balance.
    </p>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="info" :title="info" type="success" show-icon :closable="true" @close="info = ''" />
    <div class="platform-grid">
      <article v-for="row in platformRows" :key="row.preset.key" class="platform-card">
        <header>
          <h4>{{ row.preset.label }}</h4>
          <el-tag :type="row.linked ? 'success' : 'info'" size="small" effect="plain">
            {{ row.linked ? "connected" : "not connected" }}
          </el-tag>
        </header>
        <p class="platform-models">
          {{ row.preset.models.map((model) => model.display_name).join(" · ") }}
        </p>
        <p class="platform-account">
          {{ row.linked ? row.accountLabel || "Your account" : "Sign in with your platform account to enable these models." }}
        </p>
        <p class="platform-quota">
          Free allowance today: {{ row.remaining }} / {{ row.limit }} tokens
        </p>
        <div class="platform-actions">
          <el-button
            v-if="!row.linked"
            type="primary"
            size="small"
            @click="openDialog(row.preset.key)"
          >
            Authorize account
          </el-button>
          <el-button
            v-else
            size="small"
            text
            @click="row.provider && disconnect(row.provider.id)"
          >
            Unlink
          </el-button>
        </div>
      </article>
    </div>
    <p v-if="!catalog" class="hint">{{ loading ? "Loading…" : "No platform catalog available." }}</p>

    <el-dialog v-model="dialogOpen" title="Authorize a platform account" width="460px">
      <el-form label-position="top">
        <el-form-item label="Platform">
          <el-input :model-value="dialogPreset" disabled />
        </el-form-item>
        <el-form-item label="Account access credential">
          <el-input
            v-model="credential"
            type="password"
            show-password
            placeholder="API key or account token issued by the platform"
          />
        </el-form-item>
        <el-form-item label="Account label (optional)">
          <el-input v-model="accountLabel" placeholder="e.g. jason@example.com" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">Cancel</el-button>
        <el-button type="primary" :loading="saving" :disabled="!credential.trim()" @click="connect">
          Authorize
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.platform-accounts {
  margin-bottom: 16px;
}
.hint {
  margin: 0 0 12px;
  color: #6b7785;
  font-size: 12px;
  line-height: 1.6;
}
.platform-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
.platform-card {
  border: 1px solid #e3e8ee;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.platform-card header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.platform-card h4 {
  margin: 0;
  font-size: 14px;
}
.platform-models,
.platform-account,
.platform-quota {
  margin: 0;
  font-size: 12px;
  color: #6b7785;
}
.platform-quota {
  color: #0e7c86;
  font-weight: 600;
}
.platform-actions {
  margin-top: 4px;
}
</style>
