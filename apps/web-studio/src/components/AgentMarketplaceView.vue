<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  declaredCapabilities,
  mergeMarketplace,
  statusTransition,
  summarizeInvocation,
  visibleCapabilities,
  type InstalledCapability,
  type LifecycleAction,
  type MarketplaceEntry,
} from "../domain/agentMarketplace";
import {
  getAgentPluginDefinitions,
  getAgentPluginInstallations,
  getProjectAgentCapabilities,
  installAgentPlugin,
  setAgentPluginInstallationStatus,
  type AgentCapabilityResponse,
  type AgentPluginDefinition,
  type AgentPluginInstallation,
} from "../services/api";

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

const entries = computed<MarketplaceEntry[]>(() =>
  mergeMarketplace(definitions.value, installations.value),
);

const visibleCapabilitiesList = computed<InstalledCapability[]>(() =>
  visibleCapabilities(capabilityResponse.value, installations.value),
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
  error.value = "";
  try {
    if (props.tenantId) {
      definitions.value = await getAgentPluginDefinitions(props.tenantId);
    } else {
      definitions.value = [];
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
  }
}

async function install(entry: MarketplaceEntry): Promise<void> {
  if (!props.tenantId || !props.projectId) return;
  submitting.value = true;
  busyKey.value = entry.definition.plugin_key;
  error.value = "";
  info.value = "";
  try {
    await installAgentPlugin(props.tenantId, props.projectId, entry.definition.id);
    info.value = entry.definition.name + " installed into the project.";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Agent plugin installation failed.";
  } finally {
    submitting.value = false;
    busyKey.value = "";
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
          {{ entry.definition.protocol }}
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
