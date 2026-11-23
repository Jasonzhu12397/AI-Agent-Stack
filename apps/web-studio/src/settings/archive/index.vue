<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { t } from "../../i18n";
import { ApiError } from "../../services/http";
import SettingsPageShell from "../shared/SettingsPageShell.vue";
import { useSettingsResource } from "../shared/useSettingsResource";
import {
  activeProjectId,
  activeTenantId,
  ensureTenantContext,
  tenantContextError,
} from "../shared/tenantContext";
import {
  deleteArchiveItem,
  fetchArchiveItems,
  restoreArchiveItem,
} from "./api";
import {
  ARCHIVE_ITEM_TYPES,
  deleteGate,
  describeError,
  isAlreadyRestored,
  pageLabels,
  restoreResultMeta,
  type ArchiveItem,
} from "./logic";

const q = ref("");
const typeFilter = ref("");
const page = ref(1);

const pendingRestoreId = ref("");
const deletingItem = ref<ArchiveItem | null>(null);
const deleteConfirmed = ref(false);
const deleteSubmitting = ref(false);
const actionError = ref("");

const resource = useSettingsResource<{ count: number; results: ArchiveItem[] }>(async () => {
  await ensureTenantContext();
  if (!activeTenantId.value) {
    throw new ApiError(400, "TENANT_CONTEXT_UNAVAILABLE", tenantContextError.value || "No tenant is available.");
  }
  if (!activeProjectId.value) {
    throw new ApiError(400, "PROJECT_CONTEXT_UNAVAILABLE", "No project is available for archived content.");
  }
  return fetchArchiveItems(activeTenantId.value, activeProjectId.value, {
    q: q.value,
    type: typeFilter.value,
    page: page.value,
  });
});

const items = computed(() => resource.data.value?.results ?? []);
const total = computed(() => resource.data.value?.count ?? items.value.length);

async function applyFilters(): Promise<void> {
  page.value = 1;
  await resource.reload();
}

async function changePage(next: number): Promise<void> {
  page.value = next;
  await resource.reload();
}

async function restore(item: ArchiveItem): Promise<void> {
  if (!activeTenantId.value || !activeProjectId.value || pendingRestoreId.value) return;
  pendingRestoreId.value = item.id;
  actionError.value = "";
  try {
    await restoreArchiveItem(activeTenantId.value, activeProjectId.value, item.type, item.id);
    await resource.reload();
  } catch (cause) {
    actionError.value = cause instanceof ApiError ? describeError(cause.code) : "Restore failed.";
  } finally {
    pendingRestoreId.value = "";
  }
}

function openDeleteDialog(item: ArchiveItem): void {
  deletingItem.value = item;
  deleteConfirmed.value = false;
  actionError.value = "";
}

const deleteGateState = computed(() => deleteGate(deleteConfirmed.value));

async function confirmDelete(): Promise<void> {
  if (!activeTenantId.value || !activeProjectId.value || !deletingItem.value) return;
  if (!deleteGateState.value.ok) {
    actionError.value = deleteGateState.value.reason;
    return;
  }
  deleteSubmitting.value = true;
  actionError.value = "";
  const item = deletingItem.value;
  try {
    await deleteArchiveItem(activeTenantId.value, activeProjectId.value, item.type, item.id);
    deletingItem.value = null;
    await resource.reload();
  } catch (cause) {
    actionError.value = cause instanceof ApiError ? describeError(cause.code) : "Permanent deletion failed.";
  } finally {
    deleteSubmitting.value = false;
  }
}

onMounted(() => void resource.reload());
void pageLabels;
</script>

<template>
  <SettingsPageShell
    title="Archived Content"
    description="Archived agents, workflows and knowledge bases for the active project. Restore keeps the original; permanent deletion is unrecoverable and requires explicit confirmation."
    :state="resource.state.value"
    :error-code="resource.errorCode.value"
    :error-message="resource.errorMessage.value"
    empty-message="No archived items match the current filters."
    denied-message="Only project members can view archived content; project administrators can restore or delete it."
    @retry="resource.reload()"
  >
    <el-card class="settings-card" shadow="never">
      <div class="settings-filter-bar">
        <el-input
          v-model="q"
          :placeholder="t('Search archived items')"
          clearable
          style="max-width: 280px"
          @keyup.enter="applyFilters"
        />
        <el-select v-model="typeFilter" :placeholder="t('All types')" clearable style="max-width: 200px" @change="applyFilters">
          <el-option v-for="type in ARCHIVE_ITEM_TYPES" :key="type" :label="type" :value="type" />
        </el-select>
        <el-button type="primary" @click="applyFilters">{{ t("Search") }}</el-button>
      </div>

      <el-table v-if="items.length > 0" :data="items" size="small">
        <el-table-column prop="name" :label="t('Name')" />
        <el-table-column prop="type" :label="t('Item type')" width="140" />
        <el-table-column :label="t('Description')">
          <template #default="{ row }">{{ row.description ?? "—" }}</template>
        </el-table-column>
        <el-table-column prop="archived_at" :label="t('Archived at')" width="200" />
        <el-table-column :label="t('Status')" width="160">
          <template #default="{ row }">
            <el-tag :type="isAlreadyRestored(row.status) ? 'success' : 'info'" effect="plain">
              {{ t(restoreResultMeta(row.status).label) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="Actions" width="220">
          <template #default="{ row }">
            <el-button
              link
              type="primary"
              :disabled="isAlreadyRestored(row.status)"
              :loading="pendingRestoreId === row.id"
              @click="restore(row)"
            >
              {{ t("Restore") }}
            </el-button>
            <el-button link type="danger" @click="openDeleteDialog(row)">
              {{ t("Permanently delete") }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else :description="t('No archived items match the current filters.')" :image-size="64" />

      <el-pagination
        v-if="total > 0"
        class="settings-pagination"
        layout="prev, pager, next, total"
        :total="total"
        :current-page="page"
        :page-size="10"
        @current-change="changePage"
      />
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

    <el-dialog v-model="deletingItem !== null" :title="t('Delete archived item')" width="520px">
      <template v-if="deletingItem">
        <p>
          <strong>{{ deletingItem.name }}</strong>
          <span class="muted">({{ deletingItem.type }} · {{ deletingItem.id }})</span>
        </p>
        <el-alert type="error" show-icon :closable="false" :description="deleteGate(false).reason" />
        <el-checkbox v-model="deleteConfirmed" class="settings-confirm-checkbox">
          {{ t("I understand this permanent deletion is unrecoverable.") }}
        </el-checkbox>
      </template>
      <template #footer>
        <el-button @click="deletingItem = null">{{ t("Cancel") }}</el-button>
        <el-button
          type="danger"
          :disabled="!deleteGateState.ok"
          :loading="deleteSubmitting"
          @click="confirmDelete"
        >
          {{ t("Permanently delete") }}
        </el-button>
      </template>
    </el-dialog>
  </SettingsPageShell>
</template>
