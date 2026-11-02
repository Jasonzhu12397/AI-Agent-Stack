<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  approvePlugin,
  createPluginConnection,
  getPluginCatalog,
  getPluginInstallations,
  installPlugin,
  registerPlugin,
  setPluginInstallationStatus,
  type PluginDefinition,
  type PluginInstallation,
} from "../services/api";

const props = defineProps<{ tenantId: string; projectId: string }>();
const catalog = ref<PluginDefinition[]>([]);
const installations = ref<PluginInstallation[]>([]);
const selectedPluginId = ref("");
const selectedPermissions = ref<string[]>([]);
const selectedInstallationId = ref("");
const connectionName = ref("");
const authType = ref<"none" | "credential_ref" | "oauth_ref">("credential_ref");
const credentialRef = ref("");
const loading = ref(false);
const submitting = ref(false);
const error = ref("");
const info = ref("");
const pluginJson = ref(JSON.stringify({
  plugin_key: "example-readonly",
  name: "Example Read-only Connector",
  description: "Example metadata-only connector registration.",
  version: "1.0.0",
  connector_type: "http",
  maintainer: "Platform Team",
  license: "Apache-2.0",
  source_url: "https://example.invalid/connector.json",
  manifest: { category: "example" },
  tools: [{
    name: "read-status",
    description: "Read a status document.",
    input_schema: {
      type: "object",
      properties: { resource_id: { type: "string" } },
      required: ["resource_id"],
      additionalProperties: false
    },
    output_schema: {
      type: "object",
      properties: { status: { type: "string" } },
      additionalProperties: false
    },
    required_permissions: ["example:status:read"],
    risk_level: "low",
    timeout_seconds: 15,
    retry_policy: { max_attempts: 2, backoff_seconds: 2 },
    idempotency_mode: "read_only"
  }]
}, null, 2));

const selectedPlugin = computed(
  () => catalog.value.find((plugin) => plugin.id === selectedPluginId.value) ?? null,
);
const availablePermissions = computed(() => {
  const permissions = selectedPlugin.value?.tools.flatMap(
    (tool) => tool.required_permissions,
  ) ?? [];
  return [...new Set(permissions)];
});
const selectedInstallation = computed(
  () => installations.value.find(
    (item) => item.id === selectedInstallationId.value,
  ) ?? installations.value[0] ?? null,
);

async function refresh(): Promise<void> {
  if (!props.tenantId) {
    catalog.value = [];
    installations.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const catalogResponse = await getPluginCatalog(props.tenantId);
    catalog.value = catalogResponse.results;
    if (!catalog.value.some((plugin) => plugin.id === selectedPluginId.value)) {
      selectedPluginId.value = catalog.value[0]?.id ?? "";
    }
    if (props.projectId) {
      const installResponse = await getPluginInstallations(
        props.tenantId,
        props.projectId,
      );
      installations.value = installResponse.results;
      if (!installations.value.some((item) => item.id === selectedInstallationId.value)) {
        selectedInstallationId.value = installations.value[0]?.id ?? "";
      }
    } else {
      installations.value = [];
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load Integration Hub.";
  } finally {
    loading.value = false;
  }
}

async function createPlugin(): Promise<void> {
  submitting.value = true;
  error.value = "";
  info.value = "";
  try {
    const payload = JSON.parse(pluginJson.value) as Record<string, unknown>;
    payload.tenant_id = props.tenantId;
    const created = await registerPlugin(payload);
    info.value = "Plugin " + created.plugin_key + "@" + created.version + " registered as Draft.";
    selectedPluginId.value = created.id;
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Invalid plugin JSON or registration failed.";
  } finally {
    submitting.value = false;
  }
}

async function reviewPlugin(plugin: PluginDefinition): Promise<void> {
  submitting.value = true;
  error.value = "";
  try {
    await approvePlugin(plugin.id);
    info.value = plugin.name + " approved. It can now be installed in a project.";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Plugin approval failed.";
  } finally {
    submitting.value = false;
  }
}

async function installSelected(): Promise<void> {
  if (!selectedPlugin.value || !props.projectId) return;
  submitting.value = true;
  error.value = "";
  try {
    const installed = await installPlugin(
      props.tenantId,
      props.projectId,
      selectedPlugin.value.id,
      selectedPermissions.value,
    );
    selectedInstallationId.value = installed.id;
    info.value = installed.plugin_name + " installed at pinned version " + installed.plugin_version + ".";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Plugin installation failed.";
  } finally {
    submitting.value = false;
  }
}

async function updateInstallation(
  installation: PluginInstallation,
  status: "installed" | "disabled" | "uninstalled",
): Promise<void> {
  if (status === "uninstalled" && !window.confirm("Uninstall " + installation.plugin_name + "?")) {
    return;
  }
  try {
    await setPluginInstallationStatus(installation.id, status);
    info.value = installation.plugin_name + ": " + status + ".";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to update installation.";
  }
}

async function addConnection(): Promise<void> {
  const installation = selectedInstallation.value;
  if (!installation || !connectionName.value.trim()) return;
  submitting.value = true;
  error.value = "";
  try {
    await createPluginConnection(installation.id, {
      name: connectionName.value.trim(),
      auth_type: authType.value,
      credential_ref: credentialRef.value.trim(),
    });
    connectionName.value = "";
    credentialRef.value = "";
    info.value = "Credential reference saved. No secret value was sent to the platform.";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to create plugin connection.";
  } finally {
    submitting.value = false;
  }
}

function riskTag(risk: string): "success" | "warning" | "danger" | "info" {
  if (risk === "low") return "success";
  if (risk === "medium") return "warning";
  if (risk === "high" || risk === "critical") return "danger";
  return "info";
}

watch(() => [props.tenantId, props.projectId], () => void refresh());
watch(selectedPluginId, () => {
  selectedPermissions.value = [];
});
onMounted(() => void refresh());
</script>

<template>
  <section class="integration-hub">
    <header class="integration-heading">
      <div>
        <p class="eyebrow">CONNECTOR REGISTRY</p>
        <h2>Integration Hub</h2>
        <p>Register, review, install and configure connector metadata. Tool execution is not enabled by this registry slice.</p>
      </div>
      <el-button plain :loading="loading" @click="refresh">Refresh catalog</el-button>
    </header>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" />
    <el-alert v-if="info" :title="info" type="success" show-icon :closable="true" />

    <div class="integration-grid">
      <section class="integration-panel">
        <div class="integration-panel-title">
          <h3>Connector catalog</h3>
          <span>{{ catalog.length }} definitions</span>
        </div>
        <div
          v-for="plugin in catalog"
          :key="plugin.id"
          class="plugin-catalog-item"
          :class="{ selected: selectedPluginId === plugin.id }"
          @click="selectedPluginId = plugin.id"
        >
          <div class="plugin-catalog-top">
            <strong>{{ plugin.name }}</strong>
            <el-tag
              :type="plugin.review_status === 'approved' ? 'success' : plugin.review_status === 'rejected' ? 'danger' : 'warning'"
              size="small"
            >{{ plugin.review_status }}</el-tag>
          </div>
          <small>{{ plugin.plugin_key }} · v{{ plugin.version }} · {{ plugin.connector_type }}</small>
          <p>{{ plugin.description }}</p>
          <div class="plugin-tool-tags">
            <el-tag v-for="tool in plugin.tools" :key="tool.id" size="small" effect="plain">
              {{ tool.name }}
            </el-tag>
          </div>
        </div>
        <el-empty v-if="!loading && !catalog.length" description="No connector definitions registered" :image-size="60" />
      </section>

      <section class="integration-panel">
        <div class="integration-panel-title">
          <h3>Connector details</h3>
          <span v-if="selectedPlugin">{{ selectedPlugin.plugin_key }}@{{ selectedPlugin.version }}</span>
        </div>
        <template v-if="selectedPlugin">
          <div class="plugin-detail-summary">
            <strong>{{ selectedPlugin.name }}</strong>
            <p>{{ selectedPlugin.description }}</p>
            <small>{{ selectedPlugin.maintainer || "No maintainer" }} · {{ selectedPlugin.license || "License not specified" }}</small>
          </div>
          <div v-for="tool in selectedPlugin.tools" :key="tool.id" class="plugin-tool-row">
            <div class="plugin-tool-heading">
              <strong>{{ tool.name }}</strong>
              <el-tag :type="riskTag(tool.risk_level)" size="small">{{ tool.risk_level }}</el-tag>
            </div>
            <p>{{ tool.description }}</p>
            <small>{{ tool.idempotency_mode }} · timeout {{ tool.timeout_seconds }}s · {{ tool.required_permissions.join(", ") || "no declared permissions" }}</small>
          </div>
          <el-button
            v-if="selectedPlugin.review_status === 'draft'"
            type="primary"
            plain
            :loading="submitting"
            @click="reviewPlugin(selectedPlugin)"
          >Approve connector metadata</el-button>
          <template v-if="selectedPlugin.review_status === 'approved' && selectedPlugin.enabled">
            <div v-if="availablePermissions.length" class="permission-grants">
              <label>Grant tool permissions explicitly</label>
              <el-checkbox-group v-model="selectedPermissions">
                <el-checkbox v-for="permission in availablePermissions" :key="permission" :value="permission">
                  {{ permission }}
                </el-checkbox>
              </el-checkbox-group>
            </div>
            <el-button
              type="primary"
              :disabled="!projectId"
              :loading="submitting"
              @click="installSelected"
            >Install into selected project</el-button>
          </template>
        </template>
        <el-empty v-else description="Select a connector to inspect its tools" :image-size="60" />
      </section>
    </div>

    <section class="integration-panel registration-panel">
      <div class="integration-panel-title">
        <h3>Register connector definition</h3>
        <span>Draft only · tenant administrator</span>
      </div>
      <el-input v-model="pluginJson" type="textarea" :rows="12" spellcheck="false" />
      <el-button type="primary" plain :loading="submitting" :disabled="!tenantId" @click="createPlugin">
        Register draft
      </el-button>
    </section>

    <section class="integration-panel installation-panel">
      <div class="integration-panel-title">
        <h3>Project installations</h3>
        <span>{{ installations.length }} installed records</span>
      </div>
      <div v-for="installation in installations" :key="installation.id" class="installation-card">
        <div class="plugin-catalog-top">
          <strong>{{ installation.plugin_name }} · {{ installation.plugin_version }}</strong>
          <el-tag :type="installation.status === 'installed' ? 'success' : 'info'" size="small">
            {{ installation.status }}
          </el-tag>
        </div>
        <small>Granted: {{ installation.granted_permissions.join(", ") || "none" }}</small>
        <div v-for="connection in installation.connections" :key="connection.id" class="connection-row">
          <span>{{ connection.name }} · {{ connection.auth_type }}</span>
          <el-tag size="small" type="info">{{ connection.status }}</el-tag>
        </div>
        <div class="installation-actions">
          <el-button
            v-if="installation.status === 'installed'"
            text
            size="small"
            @click="selectedInstallationId = installation.id"
          >Configure connection</el-button>
          <el-button
            v-if="installation.status === 'installed'"
            text
            size="small"
            @click="updateInstallation(installation, 'disabled')"
          >Disable</el-button>
          <el-button
            v-if="installation.status === 'disabled'"
            text
            size="small"
            @click="updateInstallation(installation, 'installed')"
          >Enable</el-button>
          <el-button
            v-if="installation.status !== 'uninstalled'"
            text
            type="danger"
            size="small"
            @click="updateInstallation(installation, 'uninstalled')"
          >Uninstall</el-button>
        </div>
      </div>
      <el-empty v-if="!installations.length" description="No connector installed in this project" :image-size="56" />
    </section>

    <section v-if="selectedInstallation" class="integration-panel connection-panel">
      <div class="integration-panel-title">
        <h3>Connection reference</h3>
        <span>{{ selectedInstallation.plugin_name }}</span>
      </div>
      <div class="connection-form">
        <el-input v-model="connectionName" placeholder="Connection name" />
        <el-select v-model="authType">
          <el-option label="Credential reference" value="credential_ref" />
          <el-option label="OAuth reference" value="oauth_ref" />
          <el-option label="No authentication" value="none" />
        </el-select>
        <el-input
          v-if="authType !== 'none'"
          v-model="credentialRef"
          placeholder="vault://path/to/credential"
        />
        <el-button type="primary" plain :loading="submitting" @click="addConnection">
          Save credential reference
        </el-button>
      </div>
      <p class="integration-safety-note">Only a reference URI is stored. Secret values, OAuth tokens and API keys must stay in a Secret Manager. Connector health checks and tool execution are not implemented yet.</p>
    </section>
  </section>
</template>
