<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  ensureTenantContext,
  tenants,
  projects,
  activeTenantId,
  activeProjectId,
  selectTenant,
  selectProject,
  tenantContextError,
} from "../shared/tenantContext";
import SettingsNav from "./SettingsNav.vue";

const navOpen = ref(false);

onMounted(() => {
  void ensureTenantContext();
});

const tenantMissing = computed(() => !tenantContextError.value && tenants.value.length === 0);

function onTenantChange(value: string): void {
  selectTenant(value);
  navOpen.value = false;
}

function onProjectChange(value: string): void {
  selectProject(value);
  navOpen.value = false;
}
</script>

<template>
  <section class="settings-layout">
    <header class="settings-layout__context">
      <div class="settings-layout__selectors">
        <el-select
          v-model="activeTenantId"
          aria-label="Tenant"
          :disabled="tenants.length === 0"
          @change="onTenantChange"
        >
          <el-option v-for="tenant in tenants" :key="tenant.id" :label="tenant.name" :value="tenant.id" />
        </el-select>
        <el-select
          v-model="activeProjectId"
          aria-label="Project"
          :disabled="projects.length === 0"
          @change="onProjectChange"
        >
          <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
        </el-select>
      </div>
      <button
        class="settings-nav-toggle"
        type="button"
        :aria-expanded="navOpen"
        aria-controls="settings-sidebar"
        @click="navOpen = !navOpen"
      >
        Settings menu
      </button>
    </header>

    <el-alert
      v-if="tenantMissing"
      type="info"
      show-icon
      :closable="false"
      title="No tenant available. Ask an administrator to add you to a tenant."
    />
    <el-alert
      v-else-if="tenantContextError"
      type="warning"
      show-icon
      :closable="false"
      :title="tenantContextError"
    />

    <div class="settings-layout__body">
      <aside id="settings-sidebar" class="settings-layout__sidebar" :class="{ open: navOpen }">
        <SettingsNav />
      </aside>
      <div class="settings-layout__content">
        <router-view />
      </div>
    </div>
  </section>
</template>
