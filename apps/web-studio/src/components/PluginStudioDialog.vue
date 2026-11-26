/**
 * Plugin Studio: real connection information in, generated technical contract out.
 *
 * The user never edits JSON. Discovering calls the backend, which fetches the
 * OpenAPI document or MCP tool list, validates it and returns generated
 * schemas. Publishing re-discovers and seals the credential in the backend vault.
 */
<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { t } from "../i18n";
import {
  discoverAgentPlugin,
  publishAgentPlugin,
  startAgentPluginOAuth,
  testAgentPluginStudio,
  type AgentPluginStudioDraft,
  type AgentPluginStudioRequest,
} from "../services/api";

const props = defineProps<{
  tenantId: string;
  projectId: string;
  initialCredential?: string;
}>();
const visible = defineModel<boolean>({ default: false });
const emit = defineEmits<{ published: [pluginId: string] }>();

const pluginType = ref<AgentPluginStudioRequest["plugin_type"]>("openapi");
const name = ref("");
const pluginKey = ref("");
const keyTouched = ref(false);
const description = ref("");
const baseUrl = ref("");
const documentUrl = ref("");
const documentText = ref("");
const documentOpen = ref(false);
const authType = ref<AgentPluginStudioRequest["auth_type"]>("none");
const credential = ref("");
const apiKeyHeader = ref("X-API-Key");
const apiKeyPrefix = ref("");
const tokenUrl = ref("");
const clientId = ref("");
const scope = ref("");
const oauthGrant = ref<"client_credentials" | "refresh_token" | "authorization_code">(
  "client_credentials",
);
const refreshToken = ref("");
const authorizationUrl = ref("");
const redirectUri = ref("");
const busy = ref(false);
const error = ref("");
const info = ref("");
const draft = ref<AgentPluginStudioDraft | null>(null);
const selected = ref<string[]>([]);

const oauthRedirectUri = computed(() => `${window.location.origin}/integrations`);

const typeOptions = computed(() => [
  {
    value: "openapi" as const,
    label: t("OpenAPI"),
    hint: t("Discover tools from an OpenAPI document. Schemas are generated for you."),
  },
  {
    value: "mcp" as const,
    label: t("MCP Server"),
    hint: t("Initialize the MCP server and discover its tools automatically."),
  },
  {
    value: "http_api" as const,
    label: t("HTTP API"),
    hint: t("Probe a service for its OpenAPI catalog and generate tools from it."),
  },
]);

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
}

watch(name, (value) => {
  if (!keyTouched.value) pluginKey.value = slugify(value);
});
watch(pluginKey, () => {
  keyTouched.value = true;
});
watch(pluginType, () => {
  draft.value = null;
  selected.value = [];
  error.value = "";
});
watch(visible, (open) => {
  if (!open) return;
  draft.value = null;
  selected.value = [];
  error.value = "";
  info.value = "";
  if (props.initialCredential) {
    const draftJson = window.sessionStorage.getItem("plugin-oauth-draft");
    if (draftJson) {
      try {
        const saved = JSON.parse(draftJson) as Record<string, string>;
        pluginType.value = (saved.pluginType as AgentPluginStudioRequest["plugin_type"]) || pluginType.value;
        name.value = saved.name || "";
        pluginKey.value = saved.pluginKey || "";
        baseUrl.value = saved.baseUrl || "";
        description.value = saved.description || "";
        documentUrl.value = saved.documentUrl || "";
        documentText.value = saved.documentText || "";
        tokenUrl.value = saved.tokenUrl || "";
        authorizationUrl.value = saved.authorizationUrl || "";
        clientId.value = saved.clientId || "";
        scope.value = saved.scope || "";
      } catch {
        // A malformed draft must not block the completed authorization.
      }
      window.sessionStorage.removeItem("plugin-oauth-draft");
    }
    authType.value = "oauth2";
    oauthGrant.value = "authorization_code";
    credential.value = props.initialCredential;
    info.value = t("OAuth authorization completed. The sealed credential reference is ready.");
  }
});

const authConfig = computed<Record<string, string> | undefined>(() => {
  const config: Record<string, string> = {};
  if (authType.value === "api_key") {
    config.header_name = apiKeyHeader.value;
    config.header_prefix = apiKeyPrefix.value;
    return config;
  }
  if (authType.value === "oauth2") {
    config.token_url = tokenUrl.value.trim();
    config.client_id = clientId.value.trim();
    config.scope = scope.value.trim();
    config.grant = oauthGrant.value;
    if (oauthGrant.value === "authorization_code") {
      return config;
    }
    if (oauthGrant.value === "refresh_token") {
      config.refresh_token = refreshToken.value.trim();
    }
    return config;
  }
  return undefined;
});

const requestPayload = computed<AgentPluginStudioRequest>(() => {
  const payload: AgentPluginStudioRequest = {
    plugin_type: pluginType.value,
    base_url: baseUrl.value.trim(),
    auth_type: authType.value,
  };
  const document = documentUrl.value.trim();
  if (document) payload.document_url = document;
  if (documentText.value.trim() && pluginType.value !== "mcp") {
    try {
      const parsed = JSON.parse(documentText.value) as Record<string, unknown>;
      payload.document = parsed;
    } catch {
      // Validation reports the malformed paste before any network call.
    }
  }
  if (authConfig.value) payload.auth_config = authConfig.value;
  if (authType.value !== "none") payload.credential = credential.value.trim();
  return payload;
});

function validateForm(requireName: boolean): string | null {
  if (!baseUrl.value.trim()) return t("Service base URL is required.");
  if (requireName && !name.value.trim()) return t("Plugin name is required.");
  if (requireName && !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(pluginKey.value)) {
    return t("Plugin key must be a lowercase hyphenated identifier.");
  }
  if (documentText.value.trim() && pluginType.value !== "mcp") {
    try {
      const parsed = JSON.parse(documentText.value) as Record<string, unknown>;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return t("The pasted OpenAPI document must be a JSON object.");
      }
    } catch {
      return t("The pasted OpenAPI document is not valid JSON.");
    }
  }
  if (authType.value !== "none" && !credential.value.trim()) {
    return t("Connection credential is required for this authentication mode.");
  }
  if (authType.value === "oauth2" && (!tokenUrl.value.trim() || !clientId.value.trim())) {
    return t("OAuth 2.0 needs a token URL and client ID.");
  }
  if (authType.value === "oauth2" && oauthGrant.value === "refresh_token" && !refreshToken.value.trim()) {
    return t("The refresh token is required for the refresh_token grant.");
  }
  if (authType.value === "oauth2" && oauthGrant.value === "authorization_code") {
    if (!credential.value.trim().startsWith("plugin://")) {
      return t("Authorize with the provider before publishing this plugin.");
    }
  }
  return null;
}

async function authorizeOAuth(): Promise<void> {
  error.value = "";
  info.value = "";
  if (!authorizationUrl.value.trim() || !tokenUrl.value.trim() || !clientId.value.trim()) {
    error.value = t("OAuth authorization needs authorization, token and client ID fields.");
    return;
  }
  if (!credential.value.trim()) {
    error.value = t("Enter the OAuth client secret before authorizing.");
    return;
  }
  const redirect = oauthRedirectUri.value;
  busy.value = true;
  try {
    const started = await startAgentPluginOAuth(props.tenantId, {
      authorization_url: authorizationUrl.value.trim(),
      token_url: tokenUrl.value.trim(),
      redirect_uri: redirect,
      client_id: clientId.value.trim(),
      client_secret: credential.value.trim(),
      scope: scope.value.trim(),
    });
    window.sessionStorage.setItem(
      "plugin-oauth-pending",
      JSON.stringify({ tenantId: props.tenantId }),
    );
    window.sessionStorage.setItem(
      "plugin-oauth-draft",
      JSON.stringify({
        pluginType: pluginType.value,
        name: name.value,
        pluginKey: pluginKey.value,
        baseUrl: baseUrl.value,
        description: description.value,
        documentUrl: documentUrl.value,
        documentText: documentText.value,
        tokenUrl: tokenUrl.value,
        authorizationUrl: authorizationUrl.value,
        clientId: clientId.value,
        scope: scope.value,
      }),
    );
    window.location.assign(started.authorize_url);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t("Unable to start the authorization.");
  } finally {
    busy.value = false;
  }
}

async function discover(): Promise<void> {
  error.value = "";
  info.value = "";
  const problem = validateForm(false);
  if (problem) {
    error.value = problem;
    return;
  }
  busy.value = true;
  try {
    draft.value = await discoverAgentPlugin(props.tenantId, requestPayload.value);
    selected.value = draft.value.capabilities.map((item) => item.name);
    info.value = draft.value.connection.message;
  } catch (cause) {
    draft.value = null;
    error.value = cause instanceof Error ? cause.message : t("Discovery failed.");
  } finally {
    busy.value = false;
  }
}

async function retest(): Promise<void> {
  error.value = "";
  const problem = validateForm(false);
  if (problem) {
    error.value = problem;
    return;
  }
  busy.value = true;
  try {
    const result = await testAgentPluginStudio(props.tenantId, requestPayload.value);
    info.value = result.connection.message;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t("Connection test failed.");
  } finally {
    busy.value = false;
  }
}

async function publish(): Promise<void> {
  error.value = "";
  const problem = validateForm(true);
  if (problem) {
    error.value = problem;
    return;
  }
  if (!draft.value) {
    error.value = t("Discover the service before publishing.");
    return;
  }
  if (!selected.value.length) {
    error.value = t("Select at least one capability.");
    return;
  }
  busy.value = true;
  try {
    const result = await publishAgentPlugin(props.tenantId, {
      ...requestPayload.value,
      plugin_key: pluginKey.value,
      name: name.value.trim(),
      description: description.value.trim(),
      project_id: props.projectId || undefined,
      selected_capabilities: selected.value,
    });
    info.value = result.changed
      ? `${result.definition.name} ${t("published as")} v${result.version}.`
      : `${result.definition.name} ${t("is already up to date")} (v${result.version}).`;
    emit("published", result.definition.id);
    draft.value = null;
    visible.value = false;
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : t("Publishing failed.");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="t('Add a plugin')" width="760px" top="4vh">
    <div class="studio">
      <p class="studio-lead">
        {{
          t(
            "Enter real connection information. The platform discovers the service, generates the manifest, schemas, dynamic form and permission policy, then tests the authenticated connection before publishing.",
          )
        }}
      </p>

      <el-alert v-if="error" :title="error" type="error" show-icon closable @close="error = ''" />
      <el-alert v-if="info" :title="info" type="success" show-icon closable @close="info = ''" />

      <section class="studio-section">
        <h4>{{ t("Plugin type") }}</h4>
        <div class="studio-types">
          <button
            v-for="option in typeOptions"
            :key="option.value"
            type="button"
            class="studio-type"
            :class="{ on: pluginType === option.value }"
            @click="pluginType = option.value"
          >
            <strong>{{ option.label }}</strong>
            <small>{{ option.hint }}</small>
          </button>
        </div>
      </section>

      <section class="studio-section">
        <h4>{{ t("Real connection information") }}</h4>
        <el-form label-position="top" class="studio-form">
          <div class="studio-row">
            <el-form-item :label="t('Plugin name')">
              <el-input v-model="name" :placeholder="t('e.g. GitHub, Jira, internal API')" />
            </el-form-item>
            <el-form-item :label="t('Plugin key')">
              <el-input v-model="pluginKey" placeholder="github-tools" />
            </el-form-item>
          </div>
          <el-form-item :label="t('Service base URL')">
            <el-input v-model="baseUrl" placeholder="https://api.example.com" />
          </el-form-item>
          <el-form-item v-if="pluginType !== 'mcp'">
            <el-checkbox v-model="documentOpen">{{ t("Paste the OpenAPI document instead") }}</el-checkbox>
            <el-input
              v-if="documentOpen"
              v-model="documentText"
              type="textarea"
              :rows="6"
              spellcheck="false"
              :placeholder="t('Paste the OpenAPI JSON document')"
            />
          </el-form-item>
          <el-form-item v-if="pluginType !== 'mcp'" :label="t('OpenAPI document URL (optional)')">
            <el-input
              v-model="documentUrl"
              placeholder="https://api.example.com/openapi.json"
            />
            <p class="studio-hint">
              {{
                t(
                  "Leave empty to probe /openapi.json, /swagger.json and /docs/openapi.json.",
                )
              }}
            </p>
          </el-form-item>
          <el-form-item :label="t('Authentication')">
            <el-radio-group v-model="authType">
              <el-radio-button value="none">{{ t("None") }}</el-radio-button>
              <el-radio-button value="bearer">{{ t("Bearer token") }}</el-radio-button>
              <el-radio-button value="api_key">{{ t("API key") }}</el-radio-button>
              <el-radio-button value="oauth2">{{ t("OAuth 2.0") }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <div v-if="authType === 'api_key'" class="studio-row">
            <el-form-item :label="t('Header name')">
              <el-input v-model="apiKeyHeader" placeholder="X-API-Key" />
            </el-form-item>
            <el-form-item :label="t('Value prefix (optional)')">
              <el-input v-model="apiKeyPrefix" placeholder="Bearer " />
            </el-form-item>
          </div>
          <div v-if="authType === 'oauth2'" class="studio-row">
            <label>{{ t("OAuth grant") }}</label>
            <el-select v-model="oauthGrant">
              <el-option :label="t('Client credentials')" value="client_credentials" />
              <el-option :label="t('Refresh token')" value="refresh_token" />
              <el-option :label="t('Authorization code')" value="authorization_code" />
            </el-select>
            <el-form-item
              v-if="oauthGrant === 'authorization_code'"
              :label="t('Authorization URL')"
            >
              <el-input
                v-model="authorizationUrl"
                placeholder="https://auth.example.com/oauth/authorize"
              />
            </el-form-item>
            <el-form-item :label="t('Token URL')">
              <el-input v-model="tokenUrl" placeholder="https://auth.example.com/oauth/token" />
            </el-form-item>
            <el-form-item
              v-if="oauthGrant === 'authorization_code'"
              :label="t('Redirect URI')"
            >
              <el-input :model-value="oauthRedirectUri" disabled />
              <p class="studio-hint">
                {{ t("Register this exact callback URL in the provider's OAuth application.") }}
              </p>
            </el-form-item>
            <el-form-item :label="t('Client ID')">
              <el-input v-model="clientId" placeholder="client-id" />
            </el-form-item>
            <el-form-item :label="t('Scope (optional)')">
              <el-input v-model="scope" placeholder="read:tools" />
            </el-form-item>
            <el-form-item
              v-if="oauthGrant === 'refresh_token'"
              :label="t('Refresh token')"
            >
              <el-input
                v-model="refreshToken"
                type="password"
                show-password
                :placeholder="t('Refresh token issued by the provider')"
              />
            </el-form-item>
          </div>
          <div v-if="authType === 'oauth2' && oauthGrant === 'authorization_code'" class="studio-actions">
            <el-button :loading="busy" @click="authorizeOAuth">
              {{ t("Authorize with the provider") }}
            </el-button>
            <span v-if="credential.startsWith('plugin://')" class="studio-hint">
              {{ credential }}
            </span>
          </div>
          <el-form-item
            v-if="authType !== 'none'"
            :label="authType === 'oauth2' ? t('Client secret') : t('Credential')"
          >
            <el-input
              v-model="credential"
              type="password"
              show-password
              :placeholder="authType === 'oauth2' ? t('OAuth client secret') : 'sk-...'"
            />
            <p class="studio-hint">
              {{
                t(
                  "The secret is sealed in the backend vault. Only a masked configured state is ever shown.",
                )
              }}
            </p>
          </el-form-item>
          <el-form-item :label="t('Description (optional)')">
            <el-input v-model="description" type="textarea" :rows="2" />
          </el-form-item>
        </el-form>
        <div class="studio-actions">
          <el-button type="primary" :loading="busy" @click="discover">
            {{ t("Discover capabilities & test connection") }}
          </el-button>
          <el-button v-if="draft" :loading="busy" plain @click="retest">
            {{ t("Re-test connection") }}
          </el-button>
        </div>
      </section>

      <section v-if="draft" class="studio-section">
        <h4>{{ t("Generated contract") }}</h4>
        <div class="studio-badges">
          <el-tag type="success" effect="plain">Manifest</el-tag>
          <el-tag type="success" effect="plain">Input Schema</el-tag>
          <el-tag type="success" effect="plain">Output Schema</el-tag>
          <el-tag type="success" effect="plain">Auth Schema</el-tag>
          <el-tag type="success" effect="plain">Tool Definitions</el-tag>
          <el-tag type="success" effect="plain">Dynamic Form</el-tag>
          <el-tag type="success" effect="plain">Permission Policy</el-tag>
        </div>
        <dl class="studio-meta">
          <div>
            <dt>{{ t("Protocol") }}</dt>
            <dd>{{ draft.protocol }}</dd>
          </div>
          <div>
            <dt>{{ t("Tools discovered") }}</dt>
            <dd>{{ draft.capabilities.length }}</dd>
          </div>
          <div>
            <dt>{{ t("Connection") }}</dt>
            <dd>
              {{ draft.connection.status }} · HTTP {{ draft.connection.http_status }}
            </dd>
          </div>
          <div>
            <dt>{{ t("Schema fingerprint") }}</dt>
            <dd><code>{{ draft.schema_fingerprint.slice(0, 16) }}…</code></dd>
          </div>
        </dl>

        <div class="studio-cap-head">
          <strong>{{ t("Capabilities to publish") }}</strong>
          <el-button size="small" text type="primary" @click="selected = draft.capabilities.map((item) => item.name)">
            {{ t("Select all") }}
          </el-button>
        </div>
        <el-checkbox-group v-model="selected" class="studio-caps">
          <el-checkbox
            v-for="capability in draft.capabilities"
            :key="capability.name"
            :value="capability.name"
          >
            <span class="studio-cap">
              <code>{{ capability.name }}</code>
              <small>{{ capability.description || capability.remote_operation || "" }}</small>
              <el-tag size="small" effect="plain">{{ capability.risk_level || "low" }}</el-tag>
            </span>
          </el-checkbox>
        </el-checkbox-group>

        <div v-if="draft.skipped.length" class="studio-skipped">
          <strong>{{ t("Not imported") }}</strong>
          <p v-for="entry in draft.skipped" :key="entry.method + entry.path">
            <code>{{ entry.method }} {{ entry.path }}</code> — {{ entry.reason }}
          </p>
          <p class="studio-hint">
            {{
              t(
                "Write and destructive methods require an approval policy the safe connector does not execute yet; they are listed instead of silently exposed.",
              )
            }}
          </p>
        </div>
        <div v-if="draft.manifest.permission_policy?.approval_required?.length" class="studio-policy">
          <strong>{{ t("Approval required") }}</strong>
          <p>{{ draft.manifest.permission_policy.approval_required.join(", ") }}</p>
        </div>

        <div class="studio-actions">
          <el-button
            type="primary"
            :loading="busy"
            :disabled="!selected.length"
            @click="publish"
          >
            {{ t("Publish to Plugin Registry") }}
          </el-button>
          <span class="studio-hint">
            {{
              t(
                "A changed schema is published as a new version; the version live projects already call is never overwritten.",
              )
            }}
          </span>
        </div>
      </section>
    </div>
  </el-dialog>
</template>

<style scoped>
.studio {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.studio-lead,
.studio-hint {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.studio-section {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
  padding: 14px;
}
.studio-section h4 {
  margin: 0 0 10px;
  font-size: 14px;
}
.studio-types {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.studio-type {
  text-align: left;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  padding: 10px 12px;
  background: var(--el-fill-color-lighter);
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.studio-type.on {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.studio-type small {
  color: var(--el-text-color-secondary);
  font-size: 11px;
}
.studio-form {
  margin-top: 4px;
}
.studio-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.studio-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 8px;
}
.studio-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.studio-meta {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin: 12px 0;
}
.studio-meta div {
  background: var(--el-fill-color-lighter);
  border-radius: 8px;
  padding: 8px 10px;
}
.studio-meta dt {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}
.studio-meta dd {
  margin: 2px 0 0;
  font-size: 12px;
  word-break: break-all;
}
.studio-cap-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.studio-caps {
  display: flex;
  flex-direction: column;
  max-height: 220px;
  overflow: auto;
}
.studio-cap {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.studio-cap small {
  color: var(--el-text-color-secondary);
  font-size: 11px;
}
.studio-skipped,
.studio-policy {
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--el-fill-color-lighter);
  font-size: 12px;
}
.studio-skipped p,
.studio-policy p {
  margin: 4px 0 0;
}
@media (max-width: 720px) {
  .studio-types,
  .studio-row,
  .studio-meta {
    grid-template-columns: 1fr;
  }
}
</style>
