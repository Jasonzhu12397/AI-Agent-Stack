<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  catalogInstallPayload,
  catalogStatusLabel,
  credentialHint,
  credentialRotationPayload,
  declaredCapabilities,
  filterCatalog,
  installPayload,
  mergeMarketplace,
  protocolLabel,
  statusTransition,
  summarizeInvocation,
  validateCatalogInstall,
  validateCredentialRotation,
  validateInstall,
  visibleCapabilities,
  type InstallValidation,
  type InstalledCapability,
  type LifecycleAction,
  type MarketplaceCatalogEntry,
  type MarketplaceEntry,
} from "../domain/agentMarketplace";
import {
  apiErrorCode,
  fetchMarketplaceCatalog,
  getAgentPluginDefinitions,
  getAgentPluginInstallations,
  getProjectAgentCapabilities,
  installAgentPlugin,
  installAgentPluginFromCatalog,
  rotateAgentPluginCredential,
  setAgentPluginInstallationStatus,
  type AgentCapabilityResponse,
  type AgentPluginDefinition,
  type AgentPluginInstallation,
} from "../services/api";

const PROTOCOL_OPTIONS = ["http_json", "mcp", "a2a", "openai_compatible"];

const props = defineProps<{ tenantId: string; projectId: string }>();

const definitions = ref<AgentPluginDefinition[]>([]);
const installations = ref<AgentPluginInstallation[]>([]);
const capabilityResponse = ref<AgentCapabilityResponse>({ capabilities: [] });
const loading = ref(false);
const submitting = ref(false);
const error = ref("");
const info = ref("");
const expandedPlugins = ref<Set<string>>(new Set());
const busyKey = ref("");
const credentialRefs = ref<Record<string, string>>({});
const catalogEntries = ref<MarketplaceCatalogEntry[]>([]);
const catalogLoading = ref(false);
const catalogQuery = ref("");
const catalogProtocol = ref("");
const catalogInstalledOnly = ref(false);
const catalogBusyKey = ref("");
const catalogCredentialRefs = ref<Record<string, string>>({});
const rotationRefs = ref<Record<string, string>>({});
const rotationBusyKey = ref("");

const entries = computed<MarketplaceEntry[]>(() =>
  mergeMarketplace(definitions.value, installations.value),
);

const visibleCapabilitiesList = computed<InstalledCapability[]>(() =>
  visibleCapabilities(capabilityResponse.value, installations.value),
);

const filteredCatalog = computed<MarketplaceCatalogEntry[]>(() =>
  filterCatalog(catalogEntries.value, {
    query: catalogQuery.value,
    protocol: catalogProtocol.value,
    installedOnly: catalogInstalledOnly.value,
  }),
);

function statusBadge(status: string): {
  label: string;
  type: "success" | "warning" | "danger" | "info";
} {
  switch (status) {
    case "installed":
      return { label: "Installed", type: "success" };
    case "disabled":
      return { label: "Disabled", type: "warning" };
    case "uninstalled":
      return { label: "Uninstalled", type: "info" };
    default:
      return { label: "Not installed", type: "info" };
  }
}

function riskTag(risk: string): "success" | "warning" | "danger" | "info" {
  if (risk === "low") return "success";
  if (risk === "medium") return "warning";
  if (risk === "high" || risk === "critical") return "danger";
  return "info";
}

function toneTag(tone: string): "success" | "danger" | "info" {
  if (tone === "success") return "success";
  if (tone === "danger") return "danger";
  return "info";
}

function invocationSummary(capability: InstalledCapability) {
  return summarizeInvocation(capability.last_invocation);
}

function formatSchema(schema: Record<string, unknown> | null | undefined): string {
  if (!schema || Object.keys(schema).length === 0) return "{}";
  return JSON.stringify(schema, null, 2);
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function setCredentialRef(pluginKey: string, value: string): void {
  credentialRefs.value = { ...credentialRefs.value, [pluginKey]: value };
}

function setCatalogCredentialRef(pluginKey: string, value: string): void {
  catalogCredentialRefs.value = { ...catalogCredentialRefs.value, [pluginKey]: value };
}

function catalogValidation(entry: MarketplaceCatalogEntry): InstallValidation {
  return validateCatalogInstall(
    entry,
    catalogCredentialRefs.value[entry.plugin_key] ?? "",
  );
}

function hintFor(entry: MarketplaceEntry): string {
  return credentialHint(entry.definition, entry.installation?.credential_configured ?? false);
}

function toggleExpanded(pluginKey: string): void {
  const next = new Set(expandedPlugins.value);
  if (next.has(pluginKey)) {
    next.delete(pluginKey);
  } else {
    next.add(pluginKey);
  }
  expandedPlugins.value = next;
}

async function refresh(): Promise<void> {
  loading.value = true;
  catalogLoading.value = true;
  error.value = "";
  try {
    if (props.tenantId) {
      definitions.value = await getAgentPluginDefinitions(props.tenantId);
      const catalog = await fetchMarketplaceCatalog(props.tenantId, props.projectId, {});
      catalogEntries.value = catalog.entries;
    } else {
      definitions.value = [];
      catalogEntries.value = [];
    }
    if (props.tenantId && props.projectId) {
      installations.value = await getAgentPluginInstallations(props.tenantId, props.projectId);
      capabilityResponse.value = await getProjectAgentCapabilities(
        props.tenantId,
        props.projectId,
      );
    } else {
      installations.value = [];
      capabilityResponse.value = { capabilities: [] };
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load agent marketplace.";
  } finally {
    loading.value = false;
    catalogLoading.value = false;
  }
}

async function installFromCatalog(entry: MarketplaceCatalogEntry): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  const ref = catalogCredentialRefs.value[entry.plugin_key] ?? "";
  const validation = catalogValidation(entry);
  if (!validation.ok) {
    error.value = validation.errors[0];
    info.value = "";
    return;
  }
  submitting.value = true;
  catalogBusyKey.value = entry.plugin_key;
  error.value = "";
  info.value = "";
  try {
    const payload = catalogInstallPayload(entry, ref);
    await installAgentPluginFromCatalog(props.tenantId, props.projectId, payload);
    info.value = entry.name + " installed into the project.";
    catalogCredentialRefs.value[entry.plugin_key] = "";
    await refresh();
  } catch (cause) {
    // Surface the backend detail together with its programmable error code;
    // the submitted reference is never echoed into the message or logged.
    const code = apiErrorCode(cause);
    const message = cause instanceof Error ? cause.message : "Agent plugin installation failed.";
    error.value = code ? `${message} (${code})` : message;
  } finally {
    submitting.value = false;
    catalogBusyKey.value = "";
  }
}

async function install(entry: MarketplaceEntry): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  const ref = credentialRefs.value[entry.definition.plugin_key] ?? "";
  const validation = validateInstall(entry.definition, ref);
  if (!validation.ok) {
    error.value = validation.errors[0];
    info.value = "";
    return;
  }
  submitting.value = true;
  busyKey.value = entry.definition.plugin_key;
  error.value = "";
  info.value = "";
  try {
    const payload = installPayload(entry.definition, ref);
    await installAgentPlugin(props.tenantId, props.projectId, payload.plugin_id, {
      granted_capabilities: payload.granted_capabilities,
      ...(payload.credential_ref !== undefined
        ? { credential_ref: payload.credential_ref }
        : {}),
    });
    info.value = entry.definition.name + " installed into the project.";
    credentialRefs.value[entry.definition.plugin_key] = "";
    await refresh();
  } catch (cause) {
    // Show the backend detail only; the submitted reference is never echoed
    // into the error message or logged.
    error.value = cause instanceof Error ? cause.message : "Agent plugin installation failed.";
  } finally {
    submitting.value = false;
    busyKey.value = "";
  }
}

// Point a live installation at a new credential reference. The installation,
// its grants and its bindings are untouched: rotation exists so an expired
// token does not force a reinstall.
async function rotateCredential(installationId: string, name: string): Promise<void> {
  if (!installationId || !props.projectId) return;
  const ref = rotationRefs.value[installationId] ?? "";
  const validation = validateCredentialRotation(ref);
  if (!validation.ok) {
    error.value = validation.errors[0];
    info.value = "";
    return;
  }
  submitting.value = true;
  rotationBusyKey.value = installationId;
  error.value = "";
  info.value = "";
  try {
    const payload = credentialRotationPayload(ref);
    await rotateAgentPluginCredential(installationId, payload.credential_ref);
    info.value = name + ": credential reference rotated.";
    rotationRefs.value[installationId] = "";
    await refresh();
  } catch (cause) {
    // Show the backend detail and its programmable code; the submitted
    // reference is never echoed into the message or logged.
    const code = apiErrorCode(cause);
    const message = cause instanceof Error ? cause.message : "Credential rotation failed.";
    error.value = code ? `${message} (${code})` : message;
  } finally {
    submitting.value = false;
    rotationBusyKey.value = "";
  }
}

async function changeStatus(entry: MarketplaceEntry, action: LifecycleAction): Promise<void> {
  const installation = entry.installation;
  if (!installation) return;
  const target = statusTransition(entry.installStatus, action);
  if (!target) return;
  if (
    target === "uninstalled" &&
    !window.confirm("Uninstall " + entry.definition.name + "? Its capabilities will disappear from this project.")
  ) {
    return;
  }
  submitting.value = true;
  busyKey.value = installation.id;
  error.value = "";
  info.value = "";
  try {
    await setAgentPluginInstallationStatus(installation.id, target);
    info.value = entry.definition.name + ": " + target + ".";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to update agent plugin installation.";
  } finally {
    submitting.value = false;
    busyKey.value = "";
  }
}

watch(() => [props.tenantId, props.projectId], () => void refresh());
onMounted(() => void refresh());
</script>

<template>
  <section class="integration-hub agent-marketplace">
    <header class="integration-heading">
      <div>
        <p class="eyebrow">AGENT MARKETPLACE</p>
        <h2>Agent Marketplace</h2>
        <p>Plug registered agent plugins into the current project, review their declared capabilities and recent invocation status, then enable, disable or uninstall them without leaving the page.</p>
      </div>
      <el-button plain :loading="loading" @click="refresh">Refresh catalog</el-button>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="info" :title="info" type="success" show-icon :closable="true" @close="info = ''" />

    <section class="integration-panel marketplace-browse">
      <div class="integration-panel-title">
        <h3>Browse catalog</h3>
        <span>{{ catalogEntries.length }} plugins</span>
      </div>
      <div class="marketplace-filters">
        <el-input
          v-model="catalogQuery"
          placeholder="Search name, description or capability"
          clearable
          class="marketplace-filter-input"
        />
        <el-select
          v-model="catalogProtocol"
          placeholder="Protocol"
          clearable
          class="marketplace-filter-select"
        >
          <el-option value="" label="All protocols" />
          <el-option
            v-for="option in PROTOCOL_OPTIONS"
            :key="option"
            :label="protocolLabel(option)"
            :value="option"
          />
        </el-select>
        <el-checkbox v-model="catalogInstalledOnly">Installed only</el-checkbox>
      </div>
      <div class="marketplace-catalog-grid">
        <div
          v-for="entry in filteredCatalog"
          :key="entry.plugin_key"
          class="plugin-catalog-item marketplace-card"
        >
          <div class="plugin-catalog-top">
            <strong>{{ entry.name }}</strong>
            <el-tag :type="catalogStatusLabel(entry).type" size="small">
              {{ catalogStatusLabel(entry).label }}
            </el-tag>
          </div>
          <small>
            {{ entry.plugin_key }} · v{{ entry.version }} · {{ protocolLabel(entry.protocol) }}
          </small>
          <p>{{ entry.description || "No description provided." }}</p>
          <div class="plugin-tool-tags">
            <el-tag size="small" effect="plain">
              {{ entry.capabilities.length }} capabilities
            </el-tag>
            <el-tag size="small" effect="plain">{{ protocolLabel(entry.protocol) }}</el-tag>
          </div>
          <div v-if="entry.auth_required" class="marketplace-credential">
            <el-input
              :model-value="catalogCredentialRefs[entry.plugin_key] ?? ''"
              placeholder="vault://platform/github or env://PLUGIN_SECRET_FOO"
              spellcheck="false"
              @update:model-value="setCatalogCredentialRef(entry.plugin_key, $event)"
            />
            <small
              class="marketplace-credential-hint"
              :class="{ 'marketplace-credential-hint-error': !catalogValidation(entry).ok }"
            >
              {{
                catalogValidation(entry).ok
                  ? "Enter a reference only, never a plaintext secret."
                  : catalogValidation(entry).errors[0]
              }}
            </small>
          </div>
          <div
            v-if="entry.installation_id && entry.auth_required && entry.installation_status === 'installed'"
            class="marketplace-rotation"
          >
            <el-input
              :model-value="rotationRefs[entry.installation_id] ?? ''"
              placeholder="env://PLUGIN_SECRET_MARKET_AGENT_TOKEN_V2"
              spellcheck="false"
              @update:model-value="rotationRefs[entry.installation_id] = $event"
            />
            <el-button
              size="small"
              plain
              :loading="submitting && rotationBusyKey === entry.installation_id"
              @click="rotateCredential(entry.installation_id, entry.name)"
            >
            Rotate credential
          </el-button>
            <small class="marketplace-credential-hint">
              Replaces the reference only; grants and workflow bindings are kept.
            </small>
          </div>

        <div class="installation-actions">
            <el-button
              v-if="!entry.installed"
              type="primary"
              plain
              size="small"
              :disabled="!projectId || (entry.auth_required && !catalogValidation(entry).ok)"
              :loading="submitting && catalogBusyKey === entry.plugin_key"
              @click="installFromCatalog(entry)"
            >
              Install
            </el-button>
            <el-tag v-else size="small" type="success">In project</el-tag>
          </div>
        </div>
        <el-empty
          v-if="!catalogLoading && filteredCatalog.length === 0"
          description="No plugins match the current filters"
          :image-size="60"
        />
      </div>
    </section>

    <div class="integration-grid">
      <section
        v-for="entry in entries"
        :key="entry.definition.id"
        class="plugin-catalog-item marketplace-card"
      >
        <div class="plugin-catalog-top">
          <strong>{{ entry.definition.name }}</strong>
          <el-tag :type="statusBadge(entry.installStatus).type" size="small">
            {{ statusBadge(entry.installStatus).label }}
          </el-tag>
        </div>
        <small>
          {{ entry.definition.plugin_key }} · v{{ entry.definition.version }} ·
          {{ protocolLabel(entry.definition.protocol) }}
        </small>
        <p>{{ entry.definition.description || "No description provided." }}</p>
        <div class="plugin-tool-tags">
          <el-tag size="small" effect="plain">
            {{ declaredCapabilities(entry.definition).length }} capabilities
          </el-tag>
          <el-tag size="small" effect="plain">
            {{ entry.definition.auth_required ? "Auth required" : "No auth" }}
          </el-tag>
          <el-tag
            v-if="entry.installation"
            :type="entry.installation.credential_configured ? 'success' : 'info'"
            size="small"
            effect="plain"
          >
            {{ entry.installation.credential_configured ? "Credential configured" : "No credential" }}
          </el-tag>
        </div>

        <div v-if="entry.definition.auth_required" class="marketplace-credential">
          <el-input
            :model-value="credentialRefs[entry.definition.plugin_key] ?? ''"
            placeholder="env://PLUGIN_SECRET_MARKET_AGENT_TOKEN"
            spellcheck="false"
            @update:model-value="setCredentialRef(entry.definition.plugin_key, $event)"
          />
          <small class="marketplace-credential-hint">
            {{ hintFor(entry) }} · Enter a reference only, never a plaintext secret.
          </small>
        </div>

        <div
          v-if="expandedPlugins.has(entry.definition.plugin_key)"
          class="marketplace-capability-list"
        >
          <div
            v-for="capability in declaredCapabilities(entry.definition)"
            :key="capability.name"
            class="plugin-tool-row"
          >
            <div class="plugin-tool-heading">
              <strong>{{ capability.name }}</strong>
              <el-tag v-if="capability.risk_level" :type="riskTag(capability.risk_level)" size="small">
                {{ capability.risk_level }}
              </el-tag>
            </div>
            <p>{{ capability.description || "No description provided." }}</p>
            <small>
              {{ capability.timeout_seconds ? "timeout " + capability.timeout_seconds + "s" : "default timeout" }}
            </small>
            <details class="marketplace-schema-details">
              <summary>Input schema</summary>
              <pre class="marketplace-schema">{{ formatSchema(capability.input_schema) }}</pre>
            </details>
          </div>
        </div>

        <div class="installation-actions">
          <el-button
            v-if="entry.canInstall"
            type="primary"
            plain
            size="small"
            :disabled="!projectId"
            :loading="submitting && busyKey === entry.definition.plugin_key"
            @click="install(entry)"
          >
            {{ entry.installStatus === "uninstalled" ? "Reinstall" : "Install" }}
          </el-button>
          <el-button
            v-if="entry.canEnable"
            size="small"
            :loading="submitting && busyKey === entry.installation?.id"
            @click="changeStatus(entry, 'enable')"
          >
            Enable
          </el-button>
          <el-button
            v-if="entry.canDisable"
            plain
            size="small"
            :loading="submitting && busyKey === entry.installation?.id"
            @click="changeStatus(entry, 'disable')"
          >
            Disable
          </el-button>
          <el-button
            v-if="entry.canUninstall"
            text
            type="danger"
            size="small"
            :loading="submitting && busyKey === entry.installation?.id"
            @click="changeStatus(entry, 'uninstall')"
          >
            Uninstall
          </el-button>
          <el-button
            text
            size="small"
            type="primary"
            @click="toggleExpanded(entry.definition.plugin_key)"
          >
            {{ expandedPlugins.has(entry.definition.plugin_key) ? "Hide capabilities" : "Show capabilities" }}
          </el-button>
        </div>
      </section>

      <el-empty
        v-if="!loading && entries.length === 0"
        description="No agent plugin definitions registered"
        :image-size="60"
      />
    </div>

    <section class="integration-panel installation-panel">
      <div class="integration-panel-title">
        <h3>Active capabilities</h3>
        <span>{{ visibleCapabilitiesList.length }} available</span>
      </div>
      <el-table
        v-if="visibleCapabilitiesList.length > 0"
        :data="visibleCapabilitiesList"
        size="small"
        class="marketplace-capability-table"
      >
        <el-table-column label="Capability" min-width="170">
          <template #default="scope">
            <strong>{{ scope.row.name }}</strong>
          </template>
        </el-table-column>
        <el-table-column prop="plugin_key" label="Plugin" min-width="150" />
        <el-table-column prop="plugin_version" label="Version" width="90" />
        <el-table-column prop="protocol" label="Protocol" width="100" />
        <el-table-column label="Risk" width="90">
          <template #default="scope">
            <el-tag v-if="scope.row.risk_level" :type="riskTag(scope.row.risk_level)" size="small">
              {{ scope.row.risk_level }}
            </el-tag>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="Last invocation" min-width="220">
          <template #default="scope">
            <el-tag :type="toneTag(invocationSummary(scope.row).tone)" size="small">
              {{ invocationSummary(scope.row).label }}
            </el-tag>
            <small class="marketplace-invocation-time">
              {{ formatTime(scope.row.last_invocation?.created_at) }}
            </small>
            <div v-if="invocationSummary(scope.row).errorCode" class="marketplace-error-code">
              {{ invocationSummary(scope.row).errorCode }}
            </div>
          </template>
        </el-table-column>
      </el-table>
      <el-empty
        v-else
        :description="projectId ? 'No active agent capabilities in this project' : 'Select a project to view active capabilities'"
        :image-size="56"
      />
    </section>
  </section>
</template>
