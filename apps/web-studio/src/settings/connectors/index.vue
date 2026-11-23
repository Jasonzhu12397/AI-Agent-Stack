<script setup lang="ts">
import { onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { activeTenantId, ensureTenantContext, tenantContextError } from "../shared/tenantContext";
import {
  fetchConnectors,
  installConnector,
  runHealthCheck,
  testInstallation,
  updateInstallation,
} from "./api";
import {
  describeError,
  healthLabel,
  healthTagType,
  installationIdempotencyKey,
  pageLabels,
  scopeLabel,
  summarizeTestResult,
  validateCredentialRef,
  type ConnectorEntry,
  type InstallationRecord,
  type TestOutcome,
} from "./logic";

const credentialDrafts = ref<Record<string, string>>({});
const configErrors = ref<Record<string, string>>({});
const testOutcomes = ref<Record<string, TestOutcome>>({});
const busyAction = ref<Record<string, string>>({});
const installBusy = ref("");
const installError = ref("");

const resource = useSettingsResource(async () => {
  await ensureTenantContext();
  if (!activeTenantId.value) {
    throw new ApiError(400, "TENANT_CONTEXT_UNAVAILABLE", tenantContextError.value || "No tenant is available.");
  }
  return fetchConnectors(activeTenantId.value);
});

function draftFor(entry: ConnectorEntry): string {
  return credentialDrafts.value[entry.slug] ?? entry.installation?.credential_ref ?? "";
}

function setDraft(slug: string, value: string): void {
  credentialDrafts.value[slug] = value;
}

function outcomeFor(installation: InstallationRecord): TestOutcome | null {
  return testOutcomes.value[installation.id] ?? null;
}

async function install(entry: ConnectorEntry): Promise<void> {
  if (!activeTenantId.value) return;
  installBusy.value = entry.slug;
  installError.value = "";
  try {
    await installConnector(
      activeTenantId.value,
      entry.slug,
      installationIdempotencyKey(entry.slug),
    );
    await resource.reload();
  } catch (cause) {
    installError.value = cause instanceof ApiError ? describeError(cause.code) : "Connector could not be installed.";
  } finally {
    installBusy.value = "";
  }
}

async function saveConfiguration(entry: ConnectorEntry): Promise<void> {
  if (!activeTenantId.value || !entry.installation) return;
  const installation = entry.installation;
  const draft = draftFor(entry).trim();
  const validation = validateCredentialRef(draft);
  if (!validation.ok) {
    configErrors.value[installation.id] = describeError(validation.code);
    return;
  }
  configErrors.value[installation.id] = "";
  busyAction.value[installation.id] = "save";
  try {
    await updateInstallation(activeTenantId.value, installation.id, { credential_ref: draft });
    await resource.reload();
  } catch (cause) {
    configErrors.value[installation.id] =
      cause instanceof ApiError ? `${describeError(cause.code)} (${cause.code})` : "Configuration could not be saved.";
  } finally {
    busyAction.value[installation.id] = "";
  }
}

async function runHealth(installation: InstallationRecord): Promise<void> {
  if (!activeTenantId.value) return;
  busyAction.value[installation.id] = "health";
  try {
    await runHealthCheck(activeTenantId.value, installation.id);
    await resource.reload();
  } catch (cause) {
    testOutcomes.value[installation.id] = {
      status: "failed",
      summary: cause instanceof ApiError ? cause.message : "Health check failed.",
      code: cause instanceof ApiError ? cause.code : "HEALTH_CHECK_FAILED",
    };
  } finally {
    busyAction.value[installation.id] = "";
  }
}

async function runTest(installation: InstallationRecord): Promise<void> {
  if (!activeTenantId.value) return;
  busyAction.value[installation.id] = "test";
  try {
    const payload = await testInstallation(activeTenantId.value, installation.id);
    testOutcomes.value[installation.id] = summarizeTestResult(payload);
  } catch (cause) {
    testOutcomes.value[installation.id] = {
      status: "failed",
      summary: cause instanceof ApiError ? cause.message : "Test invocation failed.",
      code: cause instanceof ApiError ? cause.code : "TEST_FAILED",
    };
  } finally {
    busyAction.value[installation.id] = "";
  }
}

onMounted(() => void resource.reload());
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Plugins & Connectors"
    description="Connector catalog, installed installations, health status, scopes and credential references. Secrets never leave the server."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="No connectors are available for this tenant yet."
    denied-message="Only tenant administrators can install or reconfigure connectors."
    @retry="resource.reload()"
  >
    <el-alert
      v-if="installError"
      class="settings-inline-error"
      type="error"
      show-icon
      :closable="true"
      :title="installError"
      @close="installError = ''"
    />

    <div class="settings-grid">
      <el-card v-for="entry in resource.data.value ?? []" :key="entry.slug" shadow="never" class="settings-card">
        <template #header>
          <div class="settings-card__title">
            <span>{{ entry.name }}</span>
            <el-tag class="settings-tag" effect="plain">{{ t(scopeLabel(entry.scope)) }}</el-tag>
            <el-tag class="settings-tag" :type="healthTagType(entry.health)" effect="plain">
              {{ t(healthLabel(entry.health)) }}
            </el-tag>
          </div>
        </template>

        <p class="muted">{{ entry.description }}</p>
        <p class="muted">
          {{ t("Connector type") }}: {{ entry.connector_type || "unknown" }} · {{ t("Installed") }}:
          {{ entry.installed ? t("Yes") : t("No") }}
        </p>

        <el-button
          v-if="!entry.installed || !entry.installation"
          type="primary"
          :loading="installBusy === entry.slug"
          @click="install(entry)"
        >
          {{ entry.installed ? t("Reinstall") : t("Install") }}
        </el-button>

        <div v-if="entry.installation" class="settings-connector-installation">
          <p class="muted">
            <el-tag :type="entry.installation.configured ? 'success' : 'info'" effect="plain">
              {{ entry.installation.configured ? t("Configured") : t("Credential reference") }}
            </el-tag>
            <span v-if="entry.installation.masked_hint"> · {{ entry.installation.masked_hint }}</span>
          </p>

          <el-form label-position="top" class="settings-form">
            <el-form-item :label="t('Credential reference')" :error="configErrors[entry.installation.id]">
              <el-input
                :model-value="draftFor(entry)"
                :placeholder="t('env://… or vault://…')"
                @update:model-value="(value: string) => setDraft(entry.slug, value)"
              />
            </el-form-item>
          </el-form>
          <p class="muted">
            {{ t("Secret values are never sent to or displayed by the browser; only an env:// or vault:// reference is accepted.") }}
          </p>

          <div class="settings-card__actions">
            <el-button
              :loading="busyAction[entry.installation.id] === 'save'"
              :disabled="!entry.installation.configured && !draftFor(entry)"
              @click="saveConfiguration(entry)"
            >
              {{ t("Save configuration") }}
            </el-button>
            <el-button
              :loading="busyAction[entry.installation.id] === 'health'"
              @click="runHealth(entry.installation)"
            >
              {{ t("Health check") }}
            </el-button>
            <el-button
              :loading="busyAction[entry.installation.id] === 'test'"
              @click="runTest(entry.installation)"
            >
              {{ t("Test connection") }}
            </el-button>
          </div>

          <el-alert
            v-if="outcomeFor(entry.installation)?.status === 'success'"
            type="success"
            show-icon
            :closable="false"
            :title="t('Connection test succeeded')"
            :description="outcomeFor(entry.installation)?.summary"
            data-connector-test-success
          />
          <el-alert
            v-else-if="outcomeFor(entry.installation)?.status === 'failed'"
            type="error"
            show-icon
            :closable="false"
            :title="`${t('Connection test failed')} (${outcomeFor(entry.installation)?.code})`"
            :description="outcomeFor(entry.installation)?.summary"
            data-connector-test-failure
          />
        </div>
      </el-card>
    </div>
  </SettingsPageShell>
</template>
