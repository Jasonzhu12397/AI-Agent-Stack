<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import {
  addTenantMember,
  getTenantMembers,
  removeTenantMember,
  updateTenantMember,
  type TenantMember,
} from "../services/api";

const props = defineProps<{ tenantId: string }>();
const members = ref<TenantMember[]>([]);
const username = ref("");
const role = ref<TenantMember["role"]>("member");
const loading = ref(false);
const submitting = ref(false);
const error = ref("");
const info = ref("");

async function refresh(): Promise<void> {
  if (!props.tenantId) {
    members.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    members.value = await getTenantMembers(props.tenantId);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to load organization members.";
  } finally {
    loading.value = false;
  }
}

async function invite(): Promise<void> {
  if (!username.value.trim()) return;
  submitting.value = true;
  error.value = "";
  info.value = "";
  try {
    await addTenantMember(props.tenantId, username.value.trim(), role.value);
    info.value = "Member added to the organization.";
    username.value = "";
    role.value = "member";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to add member.";
  } finally {
    submitting.value = false;
  }
}

async function changeRole(member: TenantMember, nextRole: TenantMember["role"]): Promise<void> {
  try {
    await updateTenantMember(props.tenantId, member.id, nextRole);
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to update member role.";
  }
}

async function remove(member: TenantMember): Promise<void> {
  if (!window.confirm("Remove " + member.username + " from this organization?")) return;
  try {
    await removeTenantMember(props.tenantId, member.id);
    info.value = member.username + " removed.";
    await refresh();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Unable to remove member.";
  }
}

watch(() => props.tenantId, () => void refresh());
onMounted(() => void refresh());
</script>

<template>
  <section class="operations-view">
    <header class="operations-heading">
      <div><p class="eyebrow">TENANT ACCESS CONTROL</p><h3>Organization Members</h3>
        <p>Manage tenant membership and role assignments through server-enforced RBAC.</p>
      </div>
      <el-button plain :loading="loading" @click="refresh">Refresh</el-button>
    </header>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="true" @close="error = ''" />
    <el-alert v-if="info" :title="info" type="success" show-icon :closable="true" @close="info = ''" />
    <div class="operations-panel">
      <h4>Add existing platform user</h4>
      <div class="member-invite-form">
        <el-input v-model="username" placeholder="Platform username" @keyup.enter="invite" />
        <el-select v-model="role" aria-label="Member role">
          <el-option label="Member" value="member" />
          <el-option label="Viewer" value="viewer" />
          <el-option label="Admin" value="admin" />
          <el-option label="Owner" value="owner" />
        </el-select>
        <el-button type="primary" :loading="submitting" :disabled="!username.trim()" @click="invite">Add member</el-button>
      </div>
      <p class="operations-note">Owner assignment and removal restrictions are enforced by the backend. Only an existing platform username can be added.</p>
    </div>
    <div class="operations-panel">
      <div class="operations-panel-heading"><h4>Tenant members</h4><span>{{ members.length }} MEMBERS</span></div>
      <el-table :data="members" size="small" class="task-table">
        <el-table-column prop="username" label="User" min-width="140" />
        <el-table-column prop="email" label="Email" min-width="180" />
        <el-table-column label="Role" min-width="130">
          <template #default="scope">
            <el-select :model-value="scope.row.role" size="small" @change="(value: TenantMember['role']) => changeRole(scope.row, value)">
              <el-option label="Owner" value="owner" /><el-option label="Admin" value="admin" />
              <el-option label="Member" value="member" /><el-option label="Viewer" value="viewer" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="Action" width="100">
          <template #default="scope"><el-button text type="danger" size="small" @click="remove(scope.row)">Remove</el-button></template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!loading && !members.length" description="No members returned for this tenant" :image-size="56" />
    </div>
  </section>
</template>
