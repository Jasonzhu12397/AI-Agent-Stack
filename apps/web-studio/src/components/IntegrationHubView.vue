<script setup lang="ts">
/**
 * Connector hub backed by the same agent-plugin definitions the chat composer
 * reads. The earlier screen listed a separate legacy plugin table, so a console
 * with seven working connectors showed "0 definitions" and offered an install
 * button for connectors the chat already had.
 */
import { computed, onMounted, ref, watch } from "vue";
import { t } from "../i18n";
import {
  createAgentPlugin,
  getAgentPluginDefinitions,
  getAgentPluginInstallations,
  getProjectAgentCapabilities,
  installAgentPlugin,
  invokeAgentPluginCapability,
  rotateAgentPluginCredential,
  setAgentPluginEnabled,
  setAgentPluginInstallationStatus,
  type AgentPluginDefinition,
  type AgentPluginInstallation,
  type AgentPluginManifestCapability,
  type InstalledAgentCapability,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();

const definitions = ref<AgentPluginDefinition[]>([]);
const installations = ref<AgentPluginInstallation[]>([]);
const capabilities = ref<InstalledAgentCapability[]>([]);
const selectedId = ref("");
const granted = ref<string[]>([]);
const credentialRef = ref("");
const loading = ref(false);
const submitting = ref(false);
const error = ref("");
const info = ref("");
const registerOpen = ref(false);
const runCapability = ref("");
const runArgs = ref("{}");
const runBusy = ref(false);
const runOutput = ref("");
const runError = ref("");
const rotateOpen = ref(false);
const activeCategory = ref("All");
const rotateRef = ref("");
const registerPlaceholders = {
  key: "example-agent",
  endpoint: "https://agent.example.com/v1",
  capabilities: "records.read, records.write",
};
const protocolOptions = ["http_json", "mcp", "a2a", "openai_compatible"];
const registerForm = ref({
  plugin_key: "",
  name: "",
  description: "",
  version: "1.0.0",
  protocol: "http_json" as AgentPluginDefinition["protocol"],
  endpoint_url: "",
  auth_required: true,
  capabilities: "",
});

const selected = computed(
  () => definitions.value.find((item) => item.id === selectedId.value) ?? null,
);
const selectedInstallation = computed(() => {
  if (!selected.value) return null;
  return (
    installations.value.find(
      (item) => item.plugin?.id === selected.value?.id && item.status !== "uninstalled",
    ) ?? null
  );
});
const installedCount = computed(
  () => installations.value.filter((item) => item.status !== "uninstalled").length,
);
function catalogMeta(definition: AgentPluginDefinition) {
  return definition.manifest?.catalog ?? {};
}

const categoryFilters = computed<string[]>(() => {
  const seen: string[] = [];
  for (const definition of definitions.value) {
    const category = catalogMeta(definition).category;
    if (category && !seen.includes(category)) seen.push(category);
  }
  return ["All", ...seen];
});

const visibleDefinitions = computed(() =>
  activeCategory.value === "All"
    ? definitions.value
    : definitions.value.filter(
        (definition) => catalogMeta(definition).category === activeCategory.value,
      ),
);

function isProvisioned(definition: AgentPluginDefinition): boolean {
  return catalogMeta(definition).availability !== "unprovisioned";
}

const selectedCapabilities = computed<AgentPluginManifestCapability[]>(() =>
  selected.value?.capability_definitions?.length
    ? selected.value.capability_definitions
    : (selected.value?.capabilities ?? []).map((name) => ({ name })),
);

function capabilityDetail(name: string): string {
  return selectedCapabilities.value.find((item) => item.name === name)?.description ?? "";
}

function capabilityRisk(name: string): string {
  return selectedCapabilities.value.find((item) => item.name === name)?.risk_level ?? "low";
}

async function refresh(): Promise<void> {
  if (!props.tenantId) {
    definitions.value = [];
    installations.value = [];
    capabilities.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    definitions.value = await getAgentPluginDefinitions(props.tenantId);
    installations.value = props.projectId
      ? await getAgentPluginInstallations(props.tenantId, props.projectId)
      : [];
    capabilities.value = props.projectId
      ? (await getProjectAgentCapabilities(props.tenantId, props.projectId)).capabilities
      : [];
    if (!definitions.value.some((item) => item.id === selectedId.value)) {
      selectedId.value = definitions.value[0]?.id ?? "";
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load connectors.";
  } finally {
    loading.value = false;
  }
}

watch(selectedId, () => {
  credentialRef.value = "";
  rotateOpen.value = false;
  rotateRef.value = "";
  granted.value = [...(selected.value?.capabilities ?? [])];
});

watch(
  () => [props.tenantId, props.projectId],
  () => void refresh(),
);
onMounted(() => void refresh());

async function toggleDefinition(): Promise<void> {
  if (!selected.value) return;
  submitting.value = true;
  error.value = "";
  try {
    const updated = await setAgentPluginEnabled(selected.value.id, !selected.value.enabled);
    info.value = updated.enabled
      ? `${updated.name} ${t("is enabled")}.`
      : `${updated.name} ${t("is disabled")}.`;
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to update the connector.";
  } finally {
    submitting.value = false;
  }
}

async function installSelected(): Promise<void> {
  if (!selected.value || !props.projectId) return;
  submitting.value = true;
  error.value = "";
  info.value = "";
  try {
    const installation = await installAgentPlugin(props.tenantId, props.projectId, selected.value.id, {
      granted_capabilities: granted.value,
      credential_ref: selected.value.auth_required ? credentialRef.value.trim() : "",
    });
    info.value = `${selected.value.name} ${t("installed with")} ${installation.granted_capabilities.length} ${t("capabilities")}.`;
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to install the connector.";
  } finally {
    submitting.value = false;
  }
}

async function registerConnector(): Promise<void> {
  if (!props.tenantId) return;
  submitting.value = true;
  error.value = "";
  info.value = "";
  try {
    const capabilityNames = registerForm.value.capabilities
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const created = await createAgentPlugin(props.tenantId, {
      plugin_key: registerForm.value.plugin_key.trim(),
      name: registerForm.value.name.trim(),
      description: registerForm.value.description.trim(),
      version: registerForm.value.version.trim(),
      protocol: registerForm.value.protocol,
      endpoint_url: registerForm.value.endpoint_url.trim(),
      auth_required: registerForm.value.auth_required,
      capabilities: capabilityNames,
    });
    info.value = `${created.name} ${t("registered")}.`;
    registerOpen.value = false;
    registerForm.value = {
      plugin_key: "",
      name: "",
      description: "",
      version: "1.0.0",
      protocol: "http_json",
      endpoint_url: "",
      auth_required: true,
      capabilities: "",
    };
    await refresh();
    selectedId.value = created.id;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to register the connector.";
  } finally {
    submitting.value = false;
  }
}

async function changeStatus(status: "installed" | "disabled" | "uninstalled"): Promise<void> {
  const installation = selectedInstallation.value;
  if (!installation) return;
  if (
    status === "uninstalled" &&
    !window.confirm(`${t("Uninstall")} ${installation.plugin?.name ?? ""}?`)
  ) {
    return;
  }
  submitting.value = true;
  error.value = "";
  try {
    await setAgentPluginInstallationStatus(installation.id, status);
    info.value = `${installation.plugin?.name ?? installation.plugin_key ?? ""}: ${status}.`;
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to update the installation.";
  } finally {
    submitting.value = false;
  }
}

function canRun(capability: AgentPluginManifestCapability): boolean {
  const installation = selectedInstallation.value;
  if (!installation || installation.status !== "installed") return false;
  return (
    installation.granted_capabilities.length === 0 ||
    installation.granted_capabilities.includes(capability.name)
  );
}

function argumentTemplate(capability: AgentPluginManifestCapability): string {
  const schema = capability.input_schema ?? {};
  const properties = (schema.properties ?? {}) as Record<string, { default?: unknown }>;
  const required = Array.isArray(schema.required) ? (schema.required as string[]) : [];
  const draft: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(properties)) {
    if (required.includes(key)) {
      draft[key] = value?.default ?? "";
    } else if (value?.default !== undefined) {
      draft[key] = value.default;
    }
  }
  return JSON.stringify(draft, null, 2);
}

function openRun(capability: AgentPluginManifestCapability): void {
  runCapability.value = capability.name;
  runArgs.value = argumentTemplate(capability);
  runOutput.value = "";
  runError.value = "";
}

async function submitRun(): Promise<void> {
  if (!selected.value || !props.projectId || !runCapability.value) return;
  let args: Record<string, unknown>;
  try {
    const parsed = JSON.parse(runArgs.value || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("Arguments must be a JSON object.");
    }
    args = parsed as Record<string, unknown>;
  } catch (cause) {
    runError.value = cause instanceof Error ? cause.message : "Invalid JSON arguments.";
    return;
  }
  runBusy.value = true;
  runError.value = "";
  runOutput.value = "";
  try {
    const response = await invokeAgentPluginCapability(
      props.tenantId,
      props.projectId,
      selected.value.id,
      runCapability.value,
      args,
    );
    runOutput.value = JSON.stringify(response.result, null, 2);
    info.value = `${runCapability.value} ${t("returned in")} ${response.latency_ms} ms.`;
    await refresh();
  } catch (cause) {
    runError.value = cause instanceof Error ? cause.message : "Capability invocation failed.";
  } finally {
    runBusy.value = false;
  }
}

async function submitRotate(): Promise<void> {
  if (!selectedInstallation.value) return;
  const reference = rotateRef.value.trim();
  if (!reference) {
    error.value = "Enter a credential reference.";
    return;
  }
  submitting.value = true;
  error.value = "";
  try {
    await rotateAgentPluginCredential(selectedInstallation.value.id, reference);
    info.value = t("credential rotated for") + " " + (selected.value?.name ?? "");
    rotateOpen.value = false;
    rotateRef.value = "";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to rotate the credential.";
  } finally {
    submitting.value = false;
  }
}

function lastInvocation(name: string): string {
  const entry = capabilities.value.find((item) => item.name === name);
  if (!entry?.last_invocation) return t("never called");
  return `${entry.last_invocation.status} · ${new Date(entry.last_invocation.created_at).toLocaleString()}`;
}
</script>

<template>
  <section class="connector-hub">
    <header class="hub-head">
      <div>
        <p class="hub-kicker">{{ t("CONNECTOR REGISTRY") }}</p>
        <h2>{{ t("Integration Hub") }}</h2>
        <p class="hub-sub">
          {{ t("Register, review and install connectors this project can call.") }}
        </p>
      </div>
      <div class="hub-head-actions">
        <el-button plain :loading="loading" @click="refresh">{{ t("Refresh catalog") }}</el-button>
        <el-button type="primary" plain @click="registerOpen = true">{{ t("Register connector") }}</el-button>
      </div>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon closable @close="error = ''" />
    <el-alert v-if="info" :title="info" type="success" show-icon closable @close="info = ''" />

    <div class="hub-grid">
      <section class="hub-panel">
        <div class="hub-panel-title">
          <h3>{{ t("Connector catalog") }}</h3>
          <span>{{ definitions.length }} {{ t("definitions") }}</span>
        </div>
        <p v-if="!loading && !definitions.length" class="hub-empty">
          {{ t("No connector definitions registered") }}
        </p>
        <div v-if="definitions.length" class="hub-filters">
          <button
            v-for="category in categoryFilters"
            :key="category"
            type="button"
            class="hub-filter"
            :class="{ on: category === activeCategory }"
            @click="activeCategory = category"
          >
            {{ category === "All" ? t("All categories") : category }}
          </button>
        </div>
        <button
          v-for="definition in visibleDefinitions"
          :key="definition.id"
          type="button"
          class="hub-row"
          :class="{ on: definition.id === selectedId }"
          @click="selectedId = definition.id"
        >
          <span class="hub-row-main">
            <strong>{{ definition.name }}</strong>
            <small>{{ definition.plugin_key }} · v{{ definition.version }}</small>
            <small v-if="catalogMeta(definition).vendor || catalogMeta(definition).category">
              {{ catalogMeta(definition).vendor || "" }}
              <template v-if="catalogMeta(definition).category">
                · {{ catalogMeta(definition).category }}
              </template>
              <template v-if="catalogMeta(definition).usage_count">
                · {{ catalogMeta(definition).usage_count }} {{ t("uses") }}
              </template>
            </small>
          </span>
          <span class="hub-row-tags">
            <el-tag v-if="!definition.enabled" size="small" type="info" effect="plain">
              {{ t("disabled") }}
            </el-tag>
            <el-tag
              v-if="
                installations.some(
                  (item) => item.plugin?.id === definition.id && item.status !== 'uninstalled',
                )
              "
              size="small"
              type="success"
              effect="plain"
            >
              {{ t("Installed") }}
            </el-tag>
            <el-tag size="small" effect="plain">{{ definition.protocol }}</el-tag>
          </span>
        </button>
      </section>

      <section class="hub-panel">
        <div class="hub-panel-title">
          <h3>{{ t("Connector details") }}</h3>
          <span v-if="selected">{{ selected.capabilities.length }} {{ t("capabilities") }}</span>
        </div>
        <p v-if="!selected" class="hub-empty">{{ t("Select a connector to inspect its tools") }}</p>
        <template v-else>
          <div class="hub-detail-head">
            <div>
              <h4>{{ selected.name }}</h4>
              <p>{{ selected.description }}</p>
            </div>
            <el-switch :model-value="selected.enabled" :loading="submitting" @change="toggleDefinition" />
          </div>
          <dl class="hub-meta">
            <div>
              <dt>{{ t("Protocol") }}</dt>
              <dd>{{ selected.protocol }}</dd>
            </div>
            <div>
              <dt>{{ t("Endpoint") }}</dt>
              <dd>
                {{
                  selected.protocol === "open_data"
                    ? t("Built into the platform")
                    : selected.endpoint_url || "—"
                }}
              </dd>
            </div>
            <div>
              <dt>{{ t("Credential") }}</dt>
              <dd>{{ selected.auth_required ? t("required") : t("not required") }}</dd>
            </div>
          </dl>

          <div class="hub-caps">
            <h5>{{ t("Capabilities") }}</h5>
            <div v-for="capability in selectedCapabilities" :key="capability.name" class="hub-cap">
              <div>
                <code>{{ capability.name }}</code>
                <small v-if="capabilityDetail(capability.name)">{{
                  capabilityDetail(capability.name)
                }}</small>
              </div>
              <div class="hub-cap-tags">
                <el-tag size="small" effect="plain">{{ capabilityRisk(capability.name) }}</el-tag>
                <small>{{ lastInvocation(capability.name) }}</small>
                <el-button
                  v-if="canRun(capability)"
                  size="small"
                  text
                  type="primary"
                  @click="openRun(capability)"
                >
                  {{ t("Run") }}
                </el-button>
              </div>
            </div>
          </div>

          <div v-if="runCapability" class="hub-run">
            <p class="hub-panel-label">{{ t("Run capability") }} · <code>{{ runCapability }}</code></p>
            <el-input
              v-model="runArgs"
              type="textarea"
              :rows="4"
              spellcheck="false"
            />
            <div class="hub-actions">
              <el-button type="primary" size="small" :loading="runBusy" @click="submitRun">
                {{ t("Run capability") }}
              </el-button>
              <el-button size="small" text @click="runCapability = ''">{{ t("Close") }}</el-button>
            </div>
            <p v-if="runError" class="hub-run-error">{{ runError }}</p>
            <pre v-if="runOutput" class="hub-run-output">{{ runOutput }}</pre>
          </div>

          <div v-if="selectedInstallation" class="hub-install">
            <p class="hub-panel-label">{{ t("Installed in this project") }}</p>
            <p class="hub-install-line">
              <el-tag
                size="small"
                :type="selectedInstallation.status === 'installed' ? 'success' : 'info'"
                effect="plain"
              >
                {{ selectedInstallation.status }}
              </el-tag>
              <span>{{ selectedInstallation.granted_capabilities.length }} {{ t("capabilities") }}</span>
              <span>{{
                selectedInstallation.credential_configured
                  ? t("credential configured")
                  : t("no credential")
              }}</span>
            </p>
            <div class="hub-actions">
              <el-button
                v-if="selectedInstallation.status === 'installed'"
                :loading="submitting"
                @click="changeStatus('disabled')"
              >
                {{ t("Disable") }}
              </el-button>
              <el-button
                v-if="selectedInstallation.status === 'disabled'"
                :loading="submitting"
                type="primary"
                @click="changeStatus('installed')"
              >
                {{ t("Enable") }}
              </el-button>
              <el-button
                v-if="selected.auth_required && selectedInstallation.status === 'installed'"
                @click="rotateOpen = !rotateOpen"
              >
                {{ t("Rotate credential") }}
              </el-button>
              <el-button :loading="submitting" @click="changeStatus('uninstalled')">{{
                t("Uninstall")
              }}</el-button>
            </div>
            <div v-if="rotateOpen" class="hub-rotate">
              <el-input
                v-model="rotateRef"
                :placeholder="t('credential reference')"
                spellcheck="false"
              />
              <el-button
                type="primary"
                size="small"
                :loading="submitting"
                @click="submitRotate"
              >
                {{ t("Save credential") }}
              </el-button>
            </div>
          </div>

          <div v-else class="hub-install">
            <p class="hub-panel-label">{{ t("Install in this project") }}</p>
            <el-checkbox-group v-model="granted" class="hub-grant">
              <el-checkbox v-for="capability in selected.capabilities" :key="capability" :value="capability">
                {{ capability }}
              </el-checkbox>
            </el-checkbox-group>
            <el-input
              v-if="selected.auth_required"
              v-model="credentialRef"
              class="hub-credential"
              :placeholder="t('Secret reference, e.g. vault://acme/wecom')"
            />
            <p v-if="!isProvisioned(selected)" class="hub-hint">
              {{ t("This connector needs an endpoint before it can be installed.") }}
            </p>
            <el-button
              type="primary"
              :loading="submitting"
              :disabled="!projectId || !granted.length || !isProvisioned(selected)"
              @click="installSelected"
            >
              {{ t("Install") }}
            </el-button>
          </div>
        </template>
      </section>
    </div>

    <el-dialog v-model="registerOpen" :title="t('Register connector')" width="520px">
      <el-form label-position="top" class="hub-form">
        <el-form-item :label="t('Plugin key')">
          <el-input v-model="registerForm.plugin_key" :placeholder="registerPlaceholders.key" />
        </el-form-item>
        <el-form-item :label="t('Name')">
          <el-input v-model="registerForm.name" />
        </el-form-item>
        <el-form-item :label="t('Description')">
          <el-input v-model="registerForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <div class="hub-form-row">
          <el-form-item :label="t('Version')">
            <el-input v-model="registerForm.version" />
          </el-form-item>
          <el-form-item :label="t('Protocol')">
            <el-select v-model="registerForm.protocol">
              <el-option v-for="option in protocolOptions" :key="option" :value="option" :label="option" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item :label="t('Endpoint')">
          <el-input v-model="registerForm.endpoint_url" :placeholder="registerPlaceholders.endpoint" />
        </el-form-item>
        <el-form-item :label="t('Capabilities')">
          <el-input v-model="registerForm.capabilities" :placeholder="registerPlaceholders.capabilities" />
        </el-form-item>
        <el-form-item :label="t('Credential')">
          <el-switch v-model="registerForm.auth_required" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="registerOpen = false">{{ t("Cancel") }}</el-button>
        <el-button type="primary" :loading="submitting" @click="registerConnector">
          {{ t("Create") }}
        </el-button>
      </template>
    </el-dialog>

    <section class="hub-panel hub-installations">
      <div class="hub-panel-title">
        <h3>{{ t("Project installations") }}</h3>
        <span>{{ installedCount }} {{ t("installed records") }}</span>
      </div>
      <p v-if="!installedCount" class="hub-empty">{{ t("No connector installed in this project") }}</p>
      <table v-else class="hub-table">
        <thead>
          <tr>
            <th>{{ t("Connector") }}</th>
            <th>{{ t("Status") }}</th>
            <th>{{ t("Granted capabilities") }}</th>
            <th>{{ t("Credential") }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="installation in installations.filter((item) => item.status !== 'uninstalled')"
            :key="installation.id"
          >
            <td>
              <strong>{{ installation.plugin?.name ?? installation.plugin_key }}</strong>
              <small>{{ installation.plugin?.plugin_key }} · v{{ installation.plugin_version }}</small>
            </td>
            <td>
              <el-tag size="small" effect="plain">{{ installation.status }}</el-tag>
            </td>
            <td>{{ installation.granted_capabilities.join(", ") || "—" }}</td>
            <td>{{ installation.credential_configured ? t("configured") : t("not configured") }}</td>
            <td>
              <el-button link @click="selectedId = installation.plugin?.id ?? selectedId">
                {{ t("Inspect") }}
              </el-button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<style scoped>
.connector-hub {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.hub-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.hub-kicker {
  margin: 0;
  font-size: 11px;
  letter-spacing: 0.14em;
  color: var(--el-text-color-secondary);
}
.hub-head h2 {
  margin: 4px 0 6px;
  font-size: 26px;
}
.hub-sub {
  margin: 0;
  color: var(--el-text-color-regular);
}
.hub-grid {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(380px, 1.6fr);
  gap: 18px;
}
.hub-panel {
  border: 1px solid var(--el-border-color-light);
  border-radius: 14px;
  padding: 16px;
  background: var(--el-bg-color);
}
.hub-head-actions {
  display: flex;
  gap: 8px;
}
.hub-form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.hub-panel-title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 10px;
}
.hub-panel-title h3 {
  margin: 0;
  font-size: 15px;
}
.hub-panel-title span,
.hub-empty {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.hub-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}
.hub-filter {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 999px;
  background: transparent;
  padding: 3px 10px;
  font-size: 12px;
  color: var(--el-text-color-regular);
  cursor: pointer;
}
.hub-filter.on {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.hub-hint {
  margin: 6px 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.hub-row {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 6px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: var(--el-fill-color-lighter);
  text-align: left;
  cursor: pointer;
}
.hub-row.on {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.hub-row-main {
  display: flex;
  flex-direction: column;
}
.hub-row-main small {
  color: var(--el-text-color-secondary);
}
.hub-row-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.hub-detail-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.hub-detail-head h4 {
  margin: 0 0 4px;
}
.hub-detail-head p {
  margin: 0;
  color: var(--el-text-color-regular);
  font-size: 13px;
}
.hub-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin: 14px 0;
}
.hub-meta div {
  background: var(--el-fill-color-lighter);
  border-radius: 10px;
  padding: 8px 10px;
}
.hub-meta dt {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.hub-meta dd {
  margin: 2px 0 0;
  font-size: 13px;
  word-break: break-all;
}
.hub-caps h5,
.hub-panel-label {
  margin: 12px 0 6px;
  font-size: 12px;
  letter-spacing: 0.06em;
  color: var(--el-text-color-secondary);
}
.hub-cap {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.hub-cap div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.hub-cap code {
  font-size: 12px;
}
.hub-cap small {
  color: var(--el-text-color-secondary);
}
.hub-cap-tags {
  align-items: flex-end;
  text-align: right;
}
.hub-rotate {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-top: 8px;
}
.hub-run {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-blank);
}
.hub-run-output {
  max-height: 260px;
  overflow: auto;
  margin: 8px 0 0;
  padding: 10px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}
.hub-run-error {
  margin: 8px 0 0;
  color: var(--el-color-danger);
  font-size: 12px;
}
.hub-install {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.hub-install-line {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  font-size: 13px;
}
.hub-grant {
  display: flex;
  flex-direction: column;
  margin-bottom: 8px;
}
.hub-credential {
  margin-bottom: 10px;
}
.hub-actions {
  display: flex;
  gap: 8px;
}
.hub-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.hub-table th {
  text-align: left;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  padding: 6px 8px;
}
.hub-table td {
  padding: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  vertical-align: top;
}
.hub-table td small {
  display: block;
  color: var(--el-text-color-secondary);
}
@media (max-width: 960px) {
  .hub-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .hub-meta {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
