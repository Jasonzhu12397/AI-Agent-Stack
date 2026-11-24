<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import { activeTenantId, ensureTenantContext, tenantContextError } from "../shared/tenantContext";
import {
  createDeletionRequest,
  fetchDeletionRequests,
  fetchGrants,
  fetchSessions,
  revokeGrant,
} from "./api";
import {
  DELETION_SCOPES,
  deletionRequestGate,
  deletionScopeLabel,
  deletionStatusMeta,
  describeError,
  pageLabels,
  revokeGrantGate,
  sessionAvailability,
  type Grant,
  type PrivacyBundle,
  type PrivacySession,
} from "./logic";

const resource = useSettingsResource<PrivacyBundle>(async () => {
    await ensureTenantContext();
    if (!activeTenantId.value) {
      throw new ApiError(400, "TENANT_CONTEXT_UNAVAILABLE", tenantContextError.value || "No tenant is available.");
    }
    const [grants, deletionRequests] = await Promise.all([
      fetchGrants(activeTenantId.value),
      fetchDeletionRequests(activeTenantId.value),
    ]);

    let sessions: PrivacySession[] | null = null;
    let sessionsAvailable = true;
    let sessionsReason = "";
    try {
      sessions = await fetchSessions(activeTenantId.value);
    } catch (cause) {
      if (cause instanceof ApiError) {
        const availability = sessionAvailability({ status: cause.status, code: cause.code });
        if (!availability.available) {
          sessionsAvailable = false;
          sessionsReason = availability.reasonCode;
        } else {
          throw cause;
        }
      } else {
        throw cause;
      }
    }
    return { grants, deletionRequests, sessions, sessionsAvailable, sessionsReason };
  },
);

const grants = computed(() => resource.data.value?.grants ?? []);
const deletionRequests = computed(() => resource.data.value?.deletionRequests ?? []);
const sessions = computed(() => resource.data.value?.sessions ?? null);
const sessionsAvailable = computed(() => resource.data.value?.sessionsAvailable ?? true);
const sessionsReason = computed(() => resource.data.value?.sessionsReason ?? "");

const revokeTarget = ref<Grant | null>(null);
const revokeConfirmed = ref(false);
const revokeSubmitting = ref(false);

const deletionDialogVisible = ref(false);
const deletionScope = ref<string>(DELETION_SCOPES[0]);
const deletionConfirmed = ref(false);
const deletionKey = ref("");
const deletionSubmitting = ref(false);
const deletionError = ref("");
const actionError = ref("");

const revokeGateState = computed(() => revokeGrantGate(revokeConfirmed.value));
const deletionGateState = computed(() =>
  deletionRequestGate({
    scope: deletionScope.value,
    confirm: deletionConfirmed.value,
    idempotencyKey: deletionKey.value,
  }),
);

function openRevokeDialog(grant: Grant): void {
  revokeTarget.value = grant;
  revokeConfirmed.value = false;
  actionError.value = "";
}

async function confirmRevoke(): Promise<void> {
  if (!activeTenantId.value || !revokeTarget.value) return;
  if (!revokeGateState.value.ok) {
    actionError.value = revokeGateState.value.reason;
    return;
  }
  revokeSubmitting.value = true;
  actionError.value = "";
  const grant = revokeTarget.value;
  try {
    await revokeGrant(activeTenantId.value, grant.id);
    revokeTarget.value = null;
    await resource.reload();
  } catch (cause) {
    actionError.value = cause instanceof ApiError ? describeError(cause.code) : "Grant revocation failed.";
  } finally {
    revokeSubmitting.value = false;
  }
}

function openDeletionDialog(): void {
  deletionScope.value = DELETION_SCOPES[0];
  deletionConfirmed.value = false;
  deletionError.value = "";
  const suffix = `${Date.now()}-${crypto.randomUUID()}`;
  deletionKey.value = `priv-del:${suffix}`;
  deletionDialogVisible.value = true;
}

async function submitDeletionRequest(): Promise<void> {
  if (!activeTenantId.value) return;
  if (!deletionGateState.value.ok) {
    deletionError.value = describeError("PRIVACY_DELETION_NOT_CONFIRMED");
    return;
  }
  deletionSubmitting.value = true;
  deletionError.value = "";
  try {
    await createDeletionRequest(activeTenantId.value, {
      scope: deletionScope.value,
      confirm: deletionConfirmed.value,
      idempotency_key: deletionKey.value,
    });
    deletionDialogVisible.value = false;
    await resource.reload();
  } catch (cause) {
    deletionError.value = cause instanceof ApiError ? describeError(cause.code) : "Deletion request failed.";
  } finally {
    deletionSubmitting.value = false;
  }
}

onMounted(() => void resource.reload());
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Privacy & Permissions"
    description="Active permission grants, scoped deletion requests and active sessions. Revocations and deletion requests are recorded in the audit log."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    :unavailable="!sessionsAvailable"
    :unavailable-code="sessionsReason"
    empty-message="No privacy grants or deletion requests are recorded for this tenant yet."
    denied-message="Only tenant members can view privacy data; tenant administrators can revoke grants or submit deletion requests."
    @retry="resource.reload()"
  >
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="settings-card__title">
          <span>{{ t("Active grants") }}</span>
        </div>
      </template>
      <el-table v-if="grants.length > 0" :data="grants" size="small">
        <el-table-column prop="subject" :label="t('Subject')" />
        <el-table-column prop="permission" :label="t('Permission')" />
        <el-table-column prop="scope" :label="t('Scope')" />
        <el-table-column prop="granted_at" :label="t('Granted at')" />
        <el-table-column label="Actions" width="140">
          <template #default="{ row }">
            <el-button link type="danger" @click="openRevokeDialog(row)">{{ t("Revoke") }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else :description="t('No grants recorded')" :image-size="64" />
    </el-card>

    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="settings-card__title">
          <span>{{ t("Deletion requests") }}</span>
          <el-button type="primary" @click="openDeletionDialog">{{ t("Request deletion") }}</el-button>
        </div>
      </template>
      <el-table v-if="deletionRequests.length > 0" :data="deletionRequests" size="small">
        <el-table-column :label="t('Deletion scope')">
          <template #default="{ row }">{{ t(deletionScopeLabel(row.scope)) }}</template>
        </el-table-column>
        <el-table-column :label="t('Status')">
          <template #default="{ row }">
            <el-tag :type="deletionStatusMeta(row.status).tagType" effect="plain">
              {{ t(deletionStatusMeta(row.status).label) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('Audit reference')">
          <template #default="{ row }">{{ row.audit_reference ?? "—" }}</template>
        </el-table-column>
        <el-table-column prop="created_at" :label="t('Granted at')" />
      </el-table>
      <el-empty v-else :description="t('No deletion requests yet')" :image-size="64" />
    </el-card>

    <el-card class="settings-card" shadow="never">
      <template #header>{{ t("Active sessions") }}</template>
      <el-alert
        v-if="!sessionsAvailable"
        type="warning"
        show-icon
        :closable="false"
        :title="t('Sessions unavailable')"
        :description="sessionsReason"
        data-sessions-unavailable
      />
      <el-table v-else-if="sessions && sessions.length > 0" :data="sessions" size="small">
        <el-table-column label="Session">
          <template #default="{ row }">
            {{ row.id }}
            <el-tag v-if="row.current" size="small" type="success">Current</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('Created')">
          <template #default="{ row }">
            {{ row.created_at }}
            <span v-if="row.created_at_source === 'derived_from_expire_date'" class="muted">
              (derived)
            </span>
          </template>
        </el-table-column>
        <el-table-column :label="t('Last active')">
          <template #default="{ row }">{{ row.last_activity ?? "—" }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-else-if="sessionsAvailable" description="No active sessions." :image-size="64" />
    </el-card>

    <el-alert
      v-if="actionError"
      class="settings-inline-error"
      type="error"
      show-icon
      :closable="true"
      :title="actionError"
      @close="actionError = ''"
    />

    <el-dialog
      :model-value="revokeTarget !== null"
      :title="t('Revoke grant')"
      width="520px"
      @update:model-value="(open: boolean) => { if (!open) revokeTarget = null }"
    >
      <template v-if="revokeTarget">
        <p>
          <strong>{{ revokeTarget.subject }}</strong>
          <span class="muted"> · {{ revokeTarget.permission }}</span>
        </p>
        <el-alert type="warning" show-icon :closable="false" :description="revokeGrantGate(false).reason" />
        <el-checkbox v-model="revokeConfirmed" class="settings-confirm-checkbox">
          {{ t("I understand this grant will be revoked immediately and recorded in the audit log.") }}
        </el-checkbox>
      </template>
      <template #footer>
        <el-button @click="revokeTarget = null">{{ t("Cancel") }}</el-button>
        <el-button type="danger" :disabled="!revokeGateState.ok" :loading="revokeSubmitting" @click="confirmRevoke">
          {{ t("Revoke grant") }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="deletionDialogVisible" :title="t('Request deletion')" width="520px">
      <el-form label-position="top">
        <el-form-item :label="t('Deletion scope')">
          <el-select v-model="deletionScope" style="max-width: 280px">
            <el-option v-for="scope in DELETION_SCOPES" :key="scope" :label="t(deletionScopeLabel(scope))" :value="scope" />
          </el-select>
        </el-form-item>
      </el-form>
      <el-checkbox v-model="deletionConfirmed" class="settings-confirm-checkbox">
        {{ t("I confirm this deletion request for the selected scope; the action is recorded in the audit log.") }}
      </el-checkbox>
      <p class="muted">Idempotency key: {{ deletionKey }}</p>
      <el-alert v-if="deletionError" type="error" show-icon :closable="false" :title="deletionError" />
      <template #footer>
        <el-button @click="deletionDialogVisible = false">{{ t("Cancel") }}</el-button>
        <el-button
          type="primary"
          :disabled="!deletionGateState.ok"
          :loading="deletionSubmitting"
          @click="submitDeletionRequest"
        >
          {{ t("Request deletion") }}
        </el-button>
      </template>
    </el-dialog>
  </SettingsPageShell>
</template>
